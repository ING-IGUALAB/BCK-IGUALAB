"""Ayudantes SOLO de pruebas para la Etapa 4A: sesión SQLite con interfaz asíncrona,
almacén de originales en memoria con inyección de fallos y fábricas de datos.

Nada de aquí se usa en producción. El almacén en memoria es un doble: no demuestra
nada sobre MinIO. SQLite no demuestra concurrencia de PostgreSQL.
"""
import hashlib
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.exceptions import ExternalServiceError
from app.models import Empresa, RolUsuario, SectorEmpresa, TipoDocumento, Usuario
from app.models.documento_ingesta import Documento
from app.schemas import MetadatosIngestaRequest
from app.services.ingesta.almacenamiento import ReferenciaOriginal, clave_pertenece_al_ambiente

BOM = b"\xef\xbb\xbf"


def sha256_de(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


class SesionSQLite:
    """Misma interfaz asíncrona que usa el servicio sobre una sesión síncrona de SQLite.
    Las consultas, restricciones e índices se ejecutan de verdad."""

    def __init__(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(
            self.engine, tables=[Usuario.__table__, Empresa.__table__, Documento.__table__]
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
    """

    def __init__(self, ambiente: str = "development", *, versionado: bool = False, sesion=None) -> None:
        self.ambiente = ambiente
        self.versionado = versionado
        self.objetos: dict[tuple[str, str | None], bytes] = {}
        self.llamadas: list[tuple[str, str]] = []
        self.fallos: dict[str, list[Exception | None]] = {}
        self.guardar_y_fallar = False
        self.eliminar_sin_efecto = False  # `eliminar` responde bien pero el objeto sigue
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
            raise error
        return ReferenciaOriginal(clave, version)

    async def leer(self, referencia: ReferenciaOriginal) -> bytes:
        error = self._entrar("leer", referencia.clave)
        if error is not None:
            raise error
        try:
            return self.objetos[(referencia.clave, referencia.version_id)]
        except KeyError:
            raise ExternalServiceError("STORAGE_OBJECT_NOT_FOUND", "El objeto solicitado no existe.") from None

    async def existe(self, referencia: ReferenciaOriginal) -> bool:
        error = self._entrar("existe", referencia.clave)
        if error is not None:
            raise error
        return (referencia.clave, referencia.version_id) in self.objetos

    async def eliminar(self, referencia: ReferenciaOriginal) -> None:
        assert clave_pertenece_al_ambiente(self.ambiente, referencia.clave), "clave ajena"
        error = self._entrar("eliminar", referencia.clave)
        if error is not None:
            raise error
        if not self.eliminar_sin_efecto:
            self.objetos.pop((referencia.clave, referencia.version_id), None)  # idempotente
