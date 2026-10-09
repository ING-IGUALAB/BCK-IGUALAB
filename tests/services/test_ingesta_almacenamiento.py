"""Etapa 4A: contrato de almacenamiento, claves por ambiente y adaptador S3/MinIO.

Nivel: unidad con un CLIENTE S3 FALSO. No hay red: estas pruebas NO demuestran que el
servidor MinIO real acepte las llamadas, ni persistencia, respaldo ni 50 MB.
"""
import asyncio
import base64
import hashlib
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

from app.exceptions import ExternalServiceError, ExternalServiceTimeoutError
from app.services.ingesta import almacenamiento_s3
from app.services.ingesta.almacenamiento import (
    ConfigAlmacenamiento,
    ReferenciaOriginal,
    clave_pertenece_al_ambiente,
    config_desde_settings,
    generar_clave_original,
    validar_ambiente,
)
from app.services.ingesta.almacenamiento_s3 import AlmacenOriginalesS3, crear_cliente_s3

ACCESO = "ACCESO-FICTICIO-123"
SECRETO = "SECRETO-FICTICIO-456"
ENDPOINT = "https://almacen.ejemplo.invalid"
BUCKET = "bucket-prueba"
BOM = b"\xef\xbb\xbf"


def config(**cambios) -> ConfigAlmacenamiento:
    valores = dict(
        endpoint_url=ENDPOINT, bucket=BUCKET, ambiente="development", access_key=ACCESO,
        secret_key=SECRETO, connect_timeout=5, read_timeout=30, operation_timeout=60,
    )
    valores.update(cambios)
    return ConfigAlmacenamiento(**valores)


def sha(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def clave_nueva(ambiente="development") -> str:
    return generar_clave_original(ambiente, uuid.uuid4())


def error_cliente(codigo="AccessDenied", estado=403, mensaje="detalle del servidor") -> ClientError:
    return ClientError(
        {"Error": {"Code": codigo, "Message": mensaje}, "ResponseMetadata": {"HTTPStatusCode": estado}},
        "Operacion",
    )


class CuerpoFalso:
    def __init__(self, datos: bytes):
        self._datos, self.cerrado = datos, False

    def read(self) -> bytes:
        return self._datos

    def close(self) -> None:
        self.cerrado = True


class ClienteS3Falso:
    """Imita lo mínimo de boto3 que usa el adaptador, y registra cada llamada."""

    def __init__(self, versionado: bool = False):
        self.versionado = versionado
        self.objetos: dict[tuple[str, str | None], bytes] = {}
        self.llamadas: list[tuple[str, dict]] = []
        self.errores: dict[str, Exception] = {}
        self.retardos: dict[str, float] = {}
        self.bloqueo: threading.Event | None = None
        self.cuerpos: list[CuerpoFalso] = []
        self._n = 0

    def _entrar(self, metodo: str, kwargs: dict) -> None:
        self.llamadas.append((metodo, dict(kwargs)))
        if self.bloqueo is not None:
            self.bloqueo.wait(5)
        if metodo in self.retardos:
            time.sleep(self.retardos[metodo])
        if metodo in self.errores:
            raise self.errores[metodo]

    def put_object(self, **kw):
        self._entrar("put_object", kw)
        version = None
        if self.versionado:
            self._n += 1
            version = f"v{self._n}"
        self.objetos[(kw["Key"], version)] = bytes(kw["Body"])
        return {"ETag": '"etag"', **({"VersionId": version} if version else {})}

    def get_object(self, **kw):
        self._entrar("get_object", kw)
        clave = (kw["Key"], kw.get("VersionId"))
        if clave not in self.objetos:
            raise error_cliente("NoSuchKey", 404)
        cuerpo = CuerpoFalso(self.objetos[clave])
        self.cuerpos.append(cuerpo)
        return {"Body": cuerpo}

    def head_object(self, **kw):
        self._entrar("head_object", kw)
        if (kw["Key"], kw.get("VersionId")) not in self.objetos:
            raise error_cliente("404", 404)
        return {}

    def delete_object(self, **kw):
        self._entrar("delete_object", kw)
        self.objetos.pop((kw["Key"], kw.get("VersionId")), None)
        return {}

    def metodos(self) -> list[str]:
        return [m for m, _ in self.llamadas]


def almacen(cliente=None, **cambios):
    cliente = cliente or ClienteS3Falso()
    return AlmacenOriginalesS3(config(**cambios), cliente), cliente


def sin_secretos(*textos: str):
    for texto in textos:
        for secreto in (ACCESO, SECRETO, ENDPOINT, "ejemplo.invalid", BUCKET):
            assert secreto not in texto, f"se filtró {secreto!r}"


# --- Claves y separación por ambiente ------------------------------------------------------------

def test_la_clave_incluye_ambiente_y_uuid_del_servidor_con_forma_fija():
    documento = uuid.uuid4()
    assert generar_clave_original("development", documento) == f"development/documentos/{documento}/original.md"


@pytest.mark.parametrize("ambiente", ["development", "qa", "uat"])
def test_ambientes_distintos_producen_claves_distintas_y_no_se_reconocen_entre_si(ambiente):
    documento = uuid.uuid4()
    clave = generar_clave_original(ambiente, documento)
    assert clave.startswith(f"{ambiente}/")
    assert clave_pertenece_al_ambiente(ambiente, clave)
    for otro in {"development", "qa", "uat"} - {ambiente}:
        assert generar_clave_original(otro, documento) != clave
        assert not clave_pertenece_al_ambiente(otro, clave)


def test_el_nombre_recibido_nunca_forma_parte_de_la_clave():
    clave = clave_nueva()
    assert "Memoria" not in clave and ".." not in clave and clave.endswith("/original.md")
    with pytest.raises(TypeError):
        generar_clave_original("development", "../../Memoria anual.md")  # no es un UUID


@pytest.mark.parametrize("ambiente", ["", "Dev", "a/b", "..", "development/", "-qa", "x" * 33, None, 5, "con espacio"])
def test_ambientes_invalidos(ambiente):
    with pytest.raises(ValueError):
        validar_ambiente(ambiente)


@pytest.mark.parametrize(
    "clave",
    [
        "development/documentos/../x/original.md",
        "development/documentos/ABCDEF12-0000-0000-0000-000000000000/original.md",
        "development/documentos/not-a-uuid/original.md",
        "development/pruebas/00000000-0000-0000-0000-000000000000/original.md",
        "/development/documentos/00000000-0000-0000-0000-000000000000/original.md",
        "development/documentos/00000000-0000-0000-0000-000000000000/original.md/extra",
        "development/documentos/00000000-0000-0000-0000-000000000000/otro.md",
        "development/documentos/00000000-0000-0000-0000-000000000000/original.md\n",
        "development/documentos/" + "0" * 600,
        "", None, 5,
    ],
)
def test_claves_que_no_tienen_la_forma_exacta_no_son_propias(clave):
    assert clave_pertenece_al_ambiente("development", clave) is False


# --- Configuración -------------------------------------------------------------------------------------

def test_la_configuracion_no_muestra_credenciales_ni_en_repr_ni_en_str():
    visible = f"{config()!r} {config()}"
    assert ACCESO not in visible and SECRETO not in visible


@pytest.mark.parametrize(
    "endpoint",
    ["http://almacen.ejemplo.invalid", "almacen.ejemplo.invalid", "ftp://x.invalid", "https://", "",
     "https://usuario:clave-secreta@almacen.invalid", "https://almacen.invalid/?token=clave-secreta",
     "https://almacen.invalid/#clave-secreta", None, 5],
)
def test_solo_se_admite_https_sin_credenciales_y_el_error_no_repite_el_valor(endpoint):
    with pytest.raises(ValueError) as capturado:
        config(endpoint_url=endpoint)
    assert "clave-secreta" not in str(capturado.value) and "almacen" not in str(capturado.value)


@pytest.mark.parametrize("cambio", [
    {"bucket": ""}, {"bucket": "AB"}, {"bucket": "Bucket-Mayusculas"}, {"bucket": "con espacio"},
    {"ambiente": "Dev"}, {"ambiente": "a/b"},
    {"access_key": ""}, {"secret_key": "   "}, {"access_key": None},
    {"region": "US East"}, {"region": ""}, {"region": 5},
    {"connect_timeout": 0}, {"read_timeout": -1}, {"operation_timeout": float("nan")},
    {"connect_timeout": True}, {"operation_timeout": "60"}, {"read_timeout": float("inf")},
])
def test_configuracion_invalida(cambio):
    with pytest.raises(ValueError):
        config(**cambio)


def test_region_opcional_y_configurable():
    assert config().region is None
    assert config(region="us-east-1").region == "us-east-1"


def ajustes(**cambios):
    valores = dict(
        MINIO_ENDPOINT_URL=ENDPOINT, MINIO_BUCKET=BUCKET, MINIO_REGION="", MINIO_ACCESS_KEY=ACCESO,
        MINIO_SECRET_KEY=SECRETO, APP_ENV="qa",
    )
    valores.update(cambios)
    return SimpleNamespace(**valores)


def test_config_desde_settings_completa():
    resultado = config_desde_settings(ajustes())
    assert (resultado.bucket, resultado.ambiente, resultado.region) == (BUCKET, "qa", None)
    assert (resultado.connect_timeout, resultado.read_timeout, resultado.operation_timeout) == (10.0, 60.0, 300.0)
    assert config_desde_settings(ajustes(MINIO_REGION=" us-east-1 ")).region == "us-east-1"


def test_config_desde_settings_nombra_lo_que_falta_sin_valores():
    with pytest.raises(ValueError) as capturado:
        config_desde_settings(ajustes(MINIO_BUCKET=None, MINIO_SECRET_KEY="  "))
    mensaje = str(capturado.value)
    assert "MINIO_BUCKET" in mensaje and "MINIO_SECRET_KEY" in mensaje
    assert "MINIO_ACCESS_KEY" not in mensaje and "MINIO_PREFIX" not in mensaje
    assert ACCESO not in mensaje and ENDPOINT not in mensaje


@pytest.mark.parametrize("ambiente", ["development", "qa", "uat", " qa "])
def test_el_ambiente_se_deriva_de_app_env(ambiente):
    assert config_desde_settings(ajustes(APP_ENV=ambiente)).ambiente == ambiente.strip()


@pytest.mark.parametrize("valor", ["prod", "production", "QA", "Development", "staging", "", "  ", None, 3])
def test_app_env_invalido_se_rechaza_sin_repetir_el_valor(valor):
    with pytest.raises(ValueError, match="APP_ENV") as capturado:
        config_desde_settings(ajustes(APP_ENV=valor))
    assert str(capturado.value) == "APP_ENV debe ser exactamente development, qa o uat."


def test_sin_app_env_en_los_ajustes_se_rechaza():
    ajuste = ajustes()
    del ajuste.APP_ENV
    with pytest.raises(ValueError, match="APP_ENV"):
        config_desde_settings(ajuste)


def test_los_plazos_salen_de_los_parametros_de_codigo_y_se_pueden_sustituir():
    from app.services.ingesta.parametros import ParametrosIngesta

    por_defecto = config_desde_settings(ajustes())
    assert (por_defecto.connect_timeout, por_defecto.read_timeout, por_defecto.operation_timeout) == (10.0, 60.0, 300.0)
    propios = ParametrosIngesta(
        minio_connect_timeout_segundos=1, minio_read_timeout_segundos=2.5, minio_operation_timeout_segundos=7
    )
    resultado = config_desde_settings(ajustes(), propios)
    assert (resultado.connect_timeout, resultado.read_timeout, resultado.operation_timeout) == (1.0, 2.5, 7.0)


def test_los_ajustes_de_la_aplicacion_declaran_los_nombres_sin_valores_por_defecto():
    from app.config import settings

    for nombre in ("MINIO_ENDPOINT_URL", "MINIO_BUCKET", "MINIO_REGION", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "APP_ENV"):
        assert hasattr(settings, nombre)
    # Ya no se leen del entorno: el ambiente se deriva de APP_ENV y los plazos están en código.
    for nombre in ("MINIO_PREFIX", "MINIO_CONNECT_TIMEOUT_SECONDS", "MINIO_READ_TIMEOUT_SECONDS", "MINIO_OPERATION_TIMEOUT_SECONDS"):
        assert not hasattr(settings, nombre)


# --- Cliente boto3: TLS, plazos, sin red --------------------------------------------------------------------

def test_el_cliente_se_crea_con_tls_verificado_plazos_y_sin_reintentos():
    with patch.object(almacenamiento_s3.boto3, "client") as fabrica:
        crear_cliente_s3(config(region="us-east-1"))
    argumentos = fabrica.call_args.kwargs
    assert fabrica.call_args.args == ("s3",)
    assert argumentos["endpoint_url"] == ENDPOINT and argumentos["endpoint_url"].startswith("https://")
    assert argumentos["verify"] is True
    assert argumentos["region_name"] == "us-east-1"
    assert (argumentos["aws_access_key_id"], argumentos["aws_secret_access_key"]) == (ACCESO, SECRETO)
    ajuste = argumentos["config"]
    assert (ajuste.connect_timeout, ajuste.read_timeout) == (5, 30)
    assert ajuste.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert ajuste.s3 == {"addressing_style": "path"}
    assert ajuste.signature_version == "s3v4"
    assert ajuste.request_checksum_calculation == "when_required"
    assert ajuste.response_checksum_validation == "when_required"


def test_el_cliente_real_se_construye_sin_llamadas_de_red_ni_region_inventada():
    cliente = crear_cliente_s3(config())
    assert cliente.meta.endpoint_url == ENDPOINT
    cliente_con_region = crear_cliente_s3(config(region="us-east-1"))
    assert cliente_con_region.meta.region_name == "us-east-1"


def test_el_adaptador_no_expone_nada_en_su_repr():
    asignado, _ = almacen()
    sin_secretos(repr(asignado))
    assert "development" in repr(asignado)
    with pytest.raises(TypeError):
        AlmacenOriginalesS3("no es una configuración", ClienteS3Falso())


# --- guardar -----------------------------------------------------------------------------------------------------

async def test_guardar_conserva_exactamente_los_bytes_incluido_el_bom_y_envia_md5():
    contenido = BOM + "# Año\r\nÑandú\r\n".encode("utf-8") + b"\x00\xff"  # bytes arbitrarios, tal cual
    # (el contenido real ya fue validado como UTF-8 antes; aquí se prueba que el adaptador no toca nada)
    asignado, cliente = almacen()
    clave = clave_nueva()
    referencia = await asignado.guardar(clave, contenido, sha(contenido))

    assert referencia == ReferenciaOriginal(clave, None)
    assert cliente.objetos[(clave, None)] == contenido and cliente.objetos[(clave, None)].startswith(BOM)
    [(metodo, kw)] = cliente.llamadas
    assert metodo == "put_object"
    assert (kw["Bucket"], kw["Key"], kw["ContentLength"]) == (BUCKET, clave, len(contenido))
    assert kw["ContentMD5"] == base64.b64encode(hashlib.md5(contenido).digest()).decode()
    assert kw["ContentType"].startswith("text/markdown")
    # Sin acceso público, ACL ni metadatos con el nombre del archivo.
    assert not {"ACL", "Metadata", "Tagging", "WebsiteRedirectLocation"} & set(kw)


async def test_guardar_devuelve_la_version_si_el_bucket_tiene_versionado():
    asignado, cliente = almacen(ClienteS3Falso(versionado=True))
    clave = clave_nueva()
    referencia = await asignado.guardar(clave, b"# a\n", sha(b"# a\n"))
    assert referencia.version_id == "v1" and (clave, "v1") in cliente.objetos


async def test_guardar_rechaza_antes_de_llamar_a_la_red():
    asignado, cliente = almacen()
    datos = b"# a\n"
    with pytest.raises(ExternalServiceError) as ajena:
        await asignado.guardar("development/pruebas/x/original.md", datos, sha(datos))
    assert ajena.value.code == "STORAGE_INVALID_KEY"
    with pytest.raises(ExternalServiceError):
        await asignado.guardar(clave_nueva("qa"), datos, sha(datos))  # clave de otro ambiente
    with pytest.raises(ValueError):
        await asignado.guardar(clave_nueva(), datos, sha(b"otro contenido"))
    with pytest.raises(ValueError):
        await asignado.guardar(clave_nueva(), datos, "ABC")
    with pytest.raises(TypeError):
        await asignado.guardar(clave_nueva(), "texto", sha(datos))
    assert cliente.llamadas == []


# --- leer / existe / eliminar ----------------------------------------------------------------------------------------

async def test_leer_devuelve_los_bytes_exactos_y_cierra_el_cuerpo():
    asignado, cliente = almacen()
    contenido = BOM + "ñ".encode() + b"\r\n"
    referencia = await asignado.guardar(clave_nueva(), contenido, sha(contenido))
    assert await asignado.leer(referencia) == contenido
    assert cliente.cuerpos and all(c.cerrado for c in cliente.cuerpos)


async def test_leer_y_existir_respetan_la_version():
    asignado, cliente = almacen(ClienteS3Falso(versionado=True))
    clave = clave_nueva()
    primera = await asignado.guardar(clave, b"uno\n", sha(b"uno\n"))
    assert await asignado.leer(primera) == b"uno\n"
    assert await asignado.existe(primera) is True
    assert await asignado.existe(ReferenciaOriginal(clave, "inexistente")) is False
    assert cliente.llamadas[-1][1]["VersionId"] == "inexistente"


async def test_leer_un_objeto_inexistente_es_un_error_controlado():
    asignado, _ = almacen()
    with pytest.raises(ExternalServiceError) as capturado:
        await asignado.leer(ReferenciaOriginal(clave_nueva()))
    assert capturado.value.code == "STORAGE_OBJECT_NOT_FOUND"
    sin_secretos(str(capturado.value), repr(capturado.value.details))


async def test_existe_distingue_ausente_de_error():
    asignado, cliente = almacen()
    referencia = ReferenciaOriginal(clave_nueva())
    assert await asignado.existe(referencia) is False
    cliente.errores["head_object"] = error_cliente("AccessDenied", 403)
    with pytest.raises(ExternalServiceError) as capturado:
        await asignado.existe(referencia)
    assert capturado.value.code == "STORAGE_ERROR"


async def test_eliminar_es_idempotente_y_solo_toca_el_objeto_indicado():
    asignado, cliente = almacen(ClienteS3Falso(versionado=True))
    propia, otra = clave_nueva(), clave_nueva()
    ref_propia = await asignado.guardar(propia, b"a\n", sha(b"a\n"))
    ref_otra = await asignado.guardar(otra, b"b\n", sha(b"b\n"))
    await asignado.eliminar(ref_propia)
    await asignado.eliminar(ref_propia)  # repetible
    await asignado.eliminar(ReferenciaOriginal(clave_nueva()))  # inexistente: tampoco es error
    assert (propia, ref_propia.version_id) not in cliente.objetos
    assert (otra, ref_otra.version_id) in cliente.objetos
    eliminaciones = [kw for m, kw in cliente.llamadas if m == "delete_object"]
    assert eliminaciones[0] == {"Bucket": BUCKET, "Key": propia, "VersionId": ref_propia.version_id}


async def test_eliminar_ignora_el_error_de_objeto_inexistente_pero_no_otros():
    asignado, cliente = almacen()
    cliente.errores["delete_object"] = error_cliente("NoSuchKey", 404)
    await asignado.eliminar(ReferenciaOriginal(clave_nueva()))
    cliente.errores["delete_object"] = error_cliente("AccessDenied", 403)
    with pytest.raises(ExternalServiceError):
        await asignado.eliminar(ReferenciaOriginal(clave_nueva()))


@pytest.mark.parametrize("operacion", ["leer", "existe", "eliminar"])
async def test_nunca_actua_sobre_objetos_ajenos_ni_claves_manipuladas(operacion):
    asignado, cliente = almacen()
    ajena = "development/documentos/../../otro-sistema/datos.md"
    for referencia in (ReferenciaOriginal(ajena), ReferenciaOriginal(clave_nueva("qa")),
                       ReferenciaOriginal(clave_nueva(), "v\n1"), ReferenciaOriginal(clave_nueva(), "x" * 300)):
        with pytest.raises(ExternalServiceError) as capturado:
            await getattr(asignado, operacion)(referencia)
        assert capturado.value.code == "STORAGE_INVALID_KEY"
    assert cliente.llamadas == []


# --- Errores sin filtraciones ------------------------------------------------------------------------------------------------

async def test_un_error_del_servidor_no_revela_credenciales_endpoint_bucket_ni_respuesta():
    asignado, cliente = almacen()
    cliente.errores["put_object"] = error_cliente(
        "AccessDenied", 403, f"clave {SECRETO} bucket {BUCKET} en {ENDPOINT} firmada https://x?X-Amz-Signature=abc"
    )
    datos = b"# secreto documental\n"
    with pytest.raises(ExternalServiceError) as capturado:
        await asignado.guardar(clave_nueva(), datos, sha(datos))
    error = capturado.value
    assert error.code == "STORAGE_ERROR" and error.message == "El almacenamiento devolvió un error."
    assert error.details == {"operacion": "guardar", "codigo_s3": "AccessDenied", "estado_http": 403}
    visible = f"{error} {error.message} {error.details!r}"
    sin_secretos(visible)
    assert "X-Amz-Signature" not in visible and "secreto documental" not in visible
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


async def test_un_codigo_s3_extrano_se_descarta_y_una_excepcion_cualquiera_solo_deja_su_clase():
    asignado, cliente = almacen()
    cliente.errores["head_object"] = error_cliente("https://secreto.invalid/?k=v con espacios", 500)
    with pytest.raises(ExternalServiceError) as extrano:
        await asignado.existe(ReferenciaOriginal(clave_nueva()))
    assert extrano.value.details == {"operacion": "existe", "codigo_s3": None, "estado_http": 500}

    cliente.errores["head_object"] = RuntimeError(f"fallo con {SECRETO} en {ENDPOINT}")
    with pytest.raises(ExternalServiceError) as generico:
        await asignado.existe(ReferenciaOriginal(clave_nueva()))
    assert generico.value.details == {"operacion": "existe", "tipo_error": "RuntimeError"}
    sin_secretos(f"{generico.value} {generico.value.details!r}")


# --- Timeouts, cancelación y bucle de eventos ----------------------------------------------------------------------------------

@pytest.mark.parametrize("excepcion", [
    ConnectTimeoutError(endpoint_url=ENDPOINT), ReadTimeoutError(endpoint_url=ENDPOINT), TimeoutError(SECRETO),
])
@pytest.mark.parametrize(("operacion", "incierto"), [("guardar", True), ("eliminar", True), ("leer", False), ("existe", False)])
async def test_timeouts_del_sdk_son_errores_504_con_resultado_incierto_donde_corresponde(excepcion, operacion, incierto):
    asignado, cliente = almacen()
    datos = b"# a\n"
    clave = clave_nueva()
    cliente.objetos[(clave, None)] = datos
    cliente.errores.update({"put_object": excepcion, "delete_object": excepcion, "get_object": excepcion, "head_object": excepcion})
    llamadas = {
        "guardar": lambda: asignado.guardar(clave_nueva(), datos, sha(datos)),
        "eliminar": lambda: asignado.eliminar(ReferenciaOriginal(clave)),
        "leer": lambda: asignado.leer(ReferenciaOriginal(clave)),
        "existe": lambda: asignado.existe(ReferenciaOriginal(clave)),
    }
    with pytest.raises(ExternalServiceTimeoutError) as capturado:
        await llamadas[operacion]()
    assert capturado.value.code == "STORAGE_TIMEOUT"
    assert capturado.value.details == {"operacion": operacion, "resultado_incierto": incierto}
    sin_secretos(f"{capturado.value} {capturado.value.details!r}")


async def test_el_plazo_de_la_operacion_se_aplica_aunque_el_sdk_siga_bloqueado():
    asignado, cliente = almacen(operation_timeout=0.05)
    cliente.retardos["put_object"] = 0.4
    datos = b"# a\n"
    inicio = time.monotonic()
    with pytest.raises(ExternalServiceTimeoutError) as capturado:
        await asignado.guardar(clave_nueva(), datos, sha(datos))
    assert time.monotonic() - inicio < 0.3  # no esperó a que terminara el hilo
    assert capturado.value.details["resultado_incierto"] is True
    await asyncio.sleep(0.5)  # deja terminar el hilo huérfano antes de la siguiente prueba


async def test_la_cancelacion_se_propaga_sin_convertirse_en_error():
    asignado, cliente = almacen()
    cliente.bloqueo = threading.Event()
    datos = b"# a\n"
    tarea = asyncio.create_task(asignado.guardar(clave_nueva(), datos, sha(datos)))
    while not cliente.llamadas:
        await asyncio.sleep(0.01)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    cliente.bloqueo.set()  # libera el hilo que el SDK no puede interrumpir


async def test_las_llamadas_del_sdk_no_bloquean_el_bucle_de_eventos():
    asignado, cliente = almacen()
    cliente.retardos["put_object"] = 0.3
    latidos = 0

    async def latido():
        nonlocal latidos
        while True:
            await asyncio.sleep(0.02)
            latidos += 1

    tarea = asyncio.create_task(latido())
    datos = b"# a\n"
    await asignado.guardar(clave_nueva(), datos, sha(datos))
    tarea.cancel()
    assert latidos >= 5  # con un SDK síncrono en el bucle serían 0


# --- Rutas restantes del adaptador y de la configuración -----------------------------------

async def test_el_adaptador_expone_su_ambiente_y_leer_distingue_ausente_de_error():
    asignado, cliente = almacen(ambiente="qa")
    assert asignado.ambiente == "qa"
    cliente.errores["get_object"] = error_cliente("AccessDenied", 403, f"detalle con {SECRETO}")
    with pytest.raises(ExternalServiceError) as capturado:
        await asignado.leer(ReferenciaOriginal(clave_nueva("qa")))
    assert capturado.value.code == "STORAGE_ERROR"  # distinto de STORAGE_OBJECT_NOT_FOUND
    assert capturado.value.details == {"operacion": "leer", "codigo_s3": "AccessDenied", "estado_http": 403}
    sin_secretos(f"{capturado.value} {capturado.value.details!r}")


def test_config_desde_settings_usa_los_ajustes_de_la_aplicacion_por_defecto(monkeypatch):
    # Se sustituyen los ajustes: así la prueba no depende de un `.env` local ni lo lee.
    monkeypatch.setattr("app.config.settings", ajustes(APP_ENV="uat"))
    resultado = config_desde_settings()
    assert (resultado.ambiente, resultado.bucket) == ("uat", BUCKET)
    monkeypatch.setattr("app.config.settings", ajustes(MINIO_ENDPOINT_URL=None))
    with pytest.raises(ValueError, match="MINIO_ENDPOINT_URL"):
        config_desde_settings()
