"""Ayudantes SOLO de pruebas para la Etapa 4A: sesión SQLite con interfaz asíncrona,
almacén de originales en memoria con inyección de fallos, cliente S3 falso con versionado y
fábricas de datos.

Nada de aquí se usa en producción. Los dobles no demuestran nada sobre MinIO. SQLite no
demuestra concurrencia de PostgreSQL.
"""
import hashlib
import threading
import uuid

from botocore.exceptions import ClientError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.exceptions import ExternalServiceError
from app.models import Empresa, RegistroAuditoria, RolUsuario, SectorEmpresa, TipoDocumento, Usuario
from app.models.documento_ingesta import Documento, OperacionIngesta
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta.almacenamiento import (
    EstadoSubida,
    ReferenciaOriginal,
    SubidaConocida,
    VersionObjeto,
    clave_pertenece_al_ambiente,
)

BOM = b"\xef\xbb\xbf"


def sha256_de(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def error_cliente(codigo="AccessDenied", estado=403, mensaje="detalle del servidor") -> ClientError:
    return ClientError(
        {"Error": {"Code": codigo, "Message": mensaje}, "ResponseMetadata": {"HTTPStatusCode": estado}},
        "Operacion",
    )


class SesionSQLite:
    """Misma interfaz asíncrona que usa el servicio sobre una sesión síncrona de SQLite.
    Las consultas, restricciones e índices se ejecutan de verdad."""

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(
            self.engine,
            tables=[Usuario.__table__, Empresa.__table__, Documento.__table__, OperacionIngesta.__table__, RegistroAuditoria.__table__],
        )
        self.sync = Session(self.engine, expire_on_commit=False)

    def add(self, objeto) -> None:
        self.sync.add(objeto)

    def in_transaction(self) -> bool:
        return self.sync.in_transaction()

    async def execute(self, *args, **kwargs):
        return self.sync.execute(*args, **kwargs)

    async def get(self, *args, **kwargs):
        return self.sync.get(*args, **kwargs)

    async def flush(self) -> None:
        self.sync.flush()

    async def commit(self) -> None:
        self.sync.commit()

    async def rollback(self) -> None:
        self.sync.rollback()

    async def refresh(self, objeto) -> None:
        self.sync.refresh(objeto)

    def cerrar(self) -> None:
        self.sync.close()
        self.engine.dispose()


async def crear_usuario(db, *, rol=RolUsuario.SUPERADMIN, habilitado=True, correo=None) -> Usuario:
    usuario = Usuario(
        nombre="Cuenta de prueba",
        correo=correo or f"{uuid.uuid4().hex[:8]}@pruebas.invalid",
        password_hash="hash-de-prueba",
        rol=rol,
        habilitado=habilitado,
    )
    db.add(usuario)
    await db.commit()
    return usuario


async def crear_empresa(db, nombre="Minera de Prueba", sector=SectorEmpresa.MINERIA, activa=True) -> Empresa:
    empresa = Empresa(nombre=nombre, sector=sector, activa=activa)
    db.add(empresa)
    await db.commit()
    return empresa


def metadatos(empresa, anio=2025, tipo=TipoDocumento.MEMORIA_ANUAL, sector=None) -> MetadatosIngestaRequest:
    return MetadatosIngestaRequest(empresa_id=empresa.id, anio=anio, tipo=tipo, sector=sector)


class AlmacenEnMemoria:
    """Doble de `AlmacenOriginales`. Registra llamadas y permite inyectar fallos.

    `fallos[operacion]` es una lista consumida en orden: cada elemento es una excepción
    a lanzar o `None` para que esa llamada funcione. `guardar_y_fallar` simula una
    subida de resultado incierto (el objeto queda creado aunque la llamada falle).
    `subidas` replica el registro del adaptador real: un fallo simple es `NO_CREADA`;
    `guardar_y_fallar`, `INCIERTA`; una prueba puede fijar cualquier estado (p. ej. `EN_CURSO`).
    Con `versionado`, eliminar SIN versión deja una marca de borrado y el objeto detrás.
    """

    def __init__(self, ambiente: str = "development", *, versionado: bool = False, sesion=None) -> None:
        self.ambiente = ambiente
        self.versionado = versionado
        self.objetos: dict[tuple[str, str | None], bytes] = {}
        self.marcas: set[tuple[str, str]] = set()
        self.llamadas: list[tuple[str, str]] = []
        self.fallos: dict[str, list[Exception | None]] = {}
        self.guardar_y_fallar = False
        self.eliminar_sin_efecto = False  # `eliminar` responde bien pero el objeto sigue
        self.subidas: dict[str, SubidaConocida] = {}
        self.sesion = sesion
        self.transaccion_abierta_en_llamada: list[bool] = []
        self._contador = 0

    # --- instrumentación ---------------------------------------------------
    def _entrar(self, operacion: str, clave: str) -> Exception | None:
        self.llamadas.append((operacion, clave))
        if self.sesion is not None:
            self.transaccion_abierta_en_llamada.append(self.sesion.in_transaction())
        cola = self.fallos.get(operacion)
        return cola.pop(0) if cola else None

    def claves(self) -> set[str]:
        return {clave for clave, _ in self.objetos}

    def poner_ajeno(self, clave: str, contenido: bytes = b"ajeno") -> None:
        self.objetos[(clave, None)] = contenido

    @staticmethod
    def _llave(referencia: ReferenciaOriginal) -> tuple[str, str | None]:
        version = None if referencia.version_id == "null" else referencia.version_id
        return referencia.clave, version

    # --- contrato ----------------------------------------------------------
    async def guardar(self, clave: str, contenido: bytes, sha256: str) -> ReferenciaOriginal:
        assert clave_pertenece_al_ambiente(self.ambiente, clave), "clave fuera del ambiente"
        assert sha256_de(contenido) == sha256
        error = self._entrar("guardar", clave)
        version = None
        if self.versionado:
            self._contador += 1
            version = f"version-{self._contador}"
        if error is None or self.guardar_y_fallar:
            assert (clave, version) not in self.objetos, "se intentó sobrescribir un objeto"
            self.objetos[(clave, version)] = bytes(contenido)
        if error is not None:
            self.subidas[clave] = SubidaConocida(
                EstadoSubida.INCIERTA if self.guardar_y_fallar else EstadoSubida.NO_CREADA
            )
            raise error
        self.subidas[clave] = SubidaConocida(EstadoSubida.CREADA, version)
        return ReferenciaOriginal(clave, version)

    async def leer(self, referencia: ReferenciaOriginal) -> bytes:
        error = self._entrar("leer", referencia.clave)
        if error is not None:
            raise error
        try:
            return self.objetos[self._llave(referencia)]
        except KeyError:
            raise ExternalServiceError("STORAGE_OBJECT_NOT_FOUND", "El objeto solicitado no existe.") from None

    async def existe(self, referencia: ReferenciaOriginal) -> bool:
        error = self._entrar("existe", referencia.clave)
        if error is not None:
            raise error
        clave, version = self._llave(referencia)
        if referencia.version_id is None and any(c == clave for c, _ in self.marcas):
            return False  # la marca de borrado es la versión actual: HEAD devuelve 404
        return (clave, version) in self.objetos

    async def eliminar(self, referencia: ReferenciaOriginal) -> None:
        assert clave_pertenece_al_ambiente(self.ambiente, referencia.clave), "clave ajena"
        error = self._entrar("eliminar", referencia.clave)
        if error is not None:
            raise error
        if self.eliminar_sin_efecto:
            return
        if referencia.version_id is None and self.versionado:
            self._contador += 1
            self.marcas.add((referencia.clave, f"marca-{self._contador}"))
            return
        self.objetos.pop(self._llave(referencia), None)  # idempotente
        self.marcas.discard((referencia.clave, referencia.version_id or ""))

    async def estado_subida(self, clave: str) -> SubidaConocida:
        self._entrar("estado_subida", clave)
        return self.subidas.get(clave, SubidaConocida(EstadoSubida.SIN_REGISTRO))

    async def listar_versiones(self, clave: str) -> list[VersionObjeto]:
        error = self._entrar("listar_versiones", clave)
        if error is not None:
            raise error
        versiones = [
            VersionObjeto(version or "null", False, len(datos))
            for (c, version), datos in self.objetos.items()
            if c == clave
        ]
        return versiones + [VersionObjeto(v, True) for c, v in sorted(self.marcas) if c == clave]


class ClienteS3Versionado:
    """boto3 falso con versionado opcional, marcas de borrado, listado paginado y una subida
    que puede quedar BLOQUEADA en su hilo (`compuerta`) para reproducir una petición que
    termina después del timeout o de la cancelación. Sin red ni credenciales."""

    def __init__(self, *, versionado: bool = True, permitir_listado: bool = True, tamano_pagina: int = 1000) -> None:
        self.versionado = versionado
        self.permitir_listado = permitir_listado
        self.tamano_pagina = tamano_pagina
        self.entradas: list[dict] = []  # en orden de creación; la última de cada clave es la actual
        self.llamadas: list[tuple[str, dict]] = []
        self.errores: dict[str, Exception] = {}
        self.error_tras_crear: Exception | None = None
        self.compuerta: threading.Event | None = None  # `put_object` espera a que se abra
        self.put_iniciado = threading.Event()
        self.put_terminado = threading.Event()
        self._n = 0
        self._candado = threading.Lock()

    def metodos(self) -> list[str]:
        return [m for m, _ in self.llamadas]

    def poner(self, clave: str, datos: bytes, version: str | None = None) -> str:
        """Crea una versión directamente (objeto ajeno o preexistente)."""
        with self._candado:
            self._n += 1
            id_version = version or (f"v{self._n}" if self.versionado else "null")
            self.entradas = [e for e in self.entradas if not (e["Key"] == clave and e["VersionId"] == id_version)]
            self.entradas.append({"Key": clave, "VersionId": id_version, "Body": bytes(datos), "Marca": False})
            return id_version

    def versiones_de(self, clave: str) -> list[dict]:
        return [e for e in self.entradas if e["Key"] == clave]

    def _actual(self, clave: str) -> dict | None:
        propias = self.versiones_de(clave)
        return propias[-1] if propias else None

    def _registrar(self, metodo: str, kw: dict) -> None:
        self.llamadas.append((metodo, dict(kw)))
        if metodo in self.errores:
            raise self.errores[metodo]

    def put_object(self, **kw):
        self.llamadas.append(("put_object", {k: v for k, v in kw.items() if k != "Body"}))
        self.put_iniciado.set()
        try:
            if self.compuerta is not None:
                assert self.compuerta.wait(10), "la compuerta de la subida nunca se abrió"
            if "put_object" in self.errores:
                raise self.errores["put_object"]
            version = self.poner(kw["Key"], bytes(kw["Body"]))
            if self.error_tras_crear is not None:  # el servidor guardó, pero la respuesta no llegó
                raise self.error_tras_crear
            return {"ETag": '"etag"', **({"VersionId": version} if self.versionado else {})}
        finally:
            self.put_terminado.set()

    def _buscar(self, kw: dict) -> dict | None:
        if kw.get("VersionId"):
            return next(
                (e for e in self.entradas if e["Key"] == kw["Key"] and e["VersionId"] == kw["VersionId"]), None
            )
        actual = self._actual(kw["Key"])
        return None if actual is None or actual["Marca"] else actual

    def head_object(self, **kw):
        self._registrar("head_object", kw)
        entrada = self._buscar(kw)
        if entrada is None or entrada["Marca"]:
            raise error_cliente("404", 404)
        return {"VersionId": entrada["VersionId"], "ContentLength": len(entrada["Body"])}

    def get_object(self, **kw):
        self._registrar("get_object", kw)
        entrada = self._buscar(kw)
        if entrada is None or entrada["Marca"]:
            raise error_cliente("NoSuchKey", 404)

        class Cuerpo:
            def read(self):
                return entrada["Body"]

            def close(self):
                pass

        return {"Body": Cuerpo()}

    def delete_object(self, **kw):
        self._registrar("delete_object", kw)
        with self._candado:
            if kw.get("VersionId"):
                self.entradas = [
                    e for e in self.entradas if not (e["Key"] == kw["Key"] and e["VersionId"] == kw["VersionId"])
                ]
            elif self.versionado:
                self._n += 1
                self.entradas.append({"Key": kw["Key"], "VersionId": f"m{self._n}", "Body": b"", "Marca": True})
            else:
                self.entradas = [e for e in self.entradas if e["Key"] != kw["Key"]]
        return {}

    def list_object_versions(self, **kw):
        self._registrar("list_object_versions", kw)
        if not self.permitir_listado:
            raise error_cliente("AccessDenied", 403)
        propias = [e for e in self.entradas if e["Key"].startswith(kw.get("Prefix", ""))]
        if kw.get("KeyMarker"):
            marcador = (kw["KeyMarker"], kw.get("VersionIdMarker"))
            posicion = next(
                (i for i, e in enumerate(propias) if (e["Key"], e["VersionId"]) == marcador), len(propias) - 1
            )
            propias = propias[posicion + 1:]
        pagina, resto = propias[: self.tamano_pagina], propias[self.tamano_pagina:]
        respuesta = {
            "IsTruncated": bool(resto),
            "Versions": [
                {"Key": e["Key"], "VersionId": e["VersionId"], "Size": len(e["Body"])}
                for e in pagina
                if not e["Marca"]
            ],
            "DeleteMarkers": [{"Key": e["Key"], "VersionId": e["VersionId"]} for e in pagina if e["Marca"]],
        }
        if resto:
            respuesta["NextKeyMarker"], respuesta["NextVersionIdMarker"] = pagina[-1]["Key"], pagina[-1]["VersionId"]
        return respuesta
