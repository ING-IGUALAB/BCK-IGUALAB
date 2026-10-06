"""PRUEBA MANUAL de MinIO con un Markdown de exactamente 50 000 000 bytes.

NO forma parte de pytest ni del arranque: hay que ejecutarla de forma explícita
(`--ejecutar`). Valida MinIO (subida, lectura por bloques, tamaño, SHA-256, eliminación);
NO valida la aceptación de 50 MB por el endpoint multipart (aún no existe el router) ni
el límite del proxy.

Qué hace:
 1. Genera en un archivo temporal local un Markdown UTF-8 válido de EXACTAMENTE
    50 000 000 bytes, por bloques (no lo carga entero en memoria).
 2. Calcula su tamaño y SHA-256 leyéndolo por bloques.
 3. Lo sube (el SDK lo lee del archivo por bloques) a
    `development/pruebas/<UUID>/prueba_50000000_bytes.md` del bucket indicado.
 4. Lo vuelve a leer del servidor por bloques y compara tamaño y SHA-256.
 5. Limpia SOLO ese objeto y lo verifica (ver «Limpieza»).
 6. Borra el archivo local. Informa POR SEPARADO cualquier limpieza pendiente, remota o
    local, con los datos necesarios para hacerla a mano.

LIMPIEZA Y VERSIONADO. No se presupone que el bucket tenga el versionado desactivado.
- Subida confirmada con `VersionId`: se elimina y se comprueba ESA versión (y solo esa).
- Subida confirmada sin `VersionId`: se elimina la clave y se comprueba que no existe.
- Resultado INCIERTO (timeout, conexión cortada, 5xx, Ctrl+C): el objeto pudo crearse y su
  versión se desconoce. Un `HEAD` 404 NO lo descarta (puede haber una versión detrás de una
  marca de borrado) y un `DELETE` sin versión solo añadiría una marca. Se listan las
  versiones de la clave EXACTA (`s3:ListBucketVersions`), se eliminan por `VersionId` y se
  comprueba que el listado queda vacío. Si no hay permiso de listado, o no se halla nada
  que pruebe que la subida no se completará después, la limpieza queda PENDIENTE (código 2)
  y se informan la clave y las comprobaciones manuales. Nunca se tocan otras claves.

PLAZOS EFECTIVOS. El script usa el SDK síncrono directamente. Solo se aplican el plazo de
CONEXIÓN (10 s) y el de LECTURA DE SOCKET (120 s sin recibir bytes). NO existe un plazo total
por operación: una subida o descarga lenta pero activa puede durar más de 120 s, y un servidor
que deje la conexión abierta sin enviar datos corta a los 120 s. `MINIO_OPERATION_TIMEOUT_SECONDS`
NO se aplica aquí. Sin reintentos: una sola petición por operación.

Qué NO hace: no crea buckets, no cambia permisos ni versionado, no toca otros objetos, no
imprime credenciales. Las credenciales NO se aceptan como argumentos (quedarían en el
historial): se leen de las variables de entorno MINIO_ACCESS_KEY y MINIO_SECRET_KEY
de la sesión actual o, si faltan, se piden sin eco. Este script no lee archivos `.env`.

Ctrl+C: la interrupción se registra como fallo, se ejecuta la limpieza y se imprime el resumen
(con la clave, la versión conocida e instrucciones si queda algo pendiente).

Código de salida: 0 todo verificado y limpio; 1 falló o se interrumpió la prueba pero quedó limpio;
2 LIMPIEZA PENDIENTE (remota o local), aunque la prueba también haya fallado.

Uso (PowerShell, desde la raíz del repositorio): ver `docs/ingesta/07-almacenamiento-minio.md`.
"""
import argparse
import base64
import codecs
import getpass
import hashlib
import os
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, SSLError

from app.services.ingesta.almacenamiento import ConfigAlmacenamiento
from app.services.ingesta.almacenamiento_s3 import crear_cliente_s3, listar_versiones_exactas

TAMANO_OBJETIVO = 50_000_000
AMBIENTE = "development"
PREFIJO = f"{AMBIENTE}/pruebas"
NOMBRE_OBJETO = "prueba_50000000_bytes.md"
BLOQUE = 1024 * 1024
CONTENT_TYPE = "text/markdown; charset=utf-8"
CONNECT_TIMEOUT_SEGUNDOS = 10
READ_TIMEOUT_SEGUNDOS = 120
_SIN_OBJETO = {"404", "NoSuchKey", "NotFound", "NoSuchVersion"}
_SIN_ENVIO = (ConnectTimeoutError, EndpointConnectionError, SSLError)

EXIT_OK, EXIT_FALLO, EXIT_LIMPIEZA_PENDIENTE = 0, 1, 2


# --- Generación y lectura por bloques ----------------------------------------------

def generar_markdown(ruta: Path, tamano: int = TAMANO_OBJETIVO) -> None:
    """Escribe un Markdown UTF-8 válido de exactamente `tamano` bytes, por bloques."""
    if isinstance(tamano, bool) or not isinstance(tamano, int) or tamano < 2:
        raise ValueError("El tamaño debe ser un entero de al menos 2 bytes.")
    cabecera = "# Prueba de almacenamiento\n\n".encode("utf-8")
    if tamano < len(cabecera) + 2:
        cabecera = b"#\n"
    escritos = 0
    bloque = bytearray()
    with ruta.open("wb") as destino:

        def volcar() -> None:
            nonlocal escritos
            destino.write(bloque)
            escritos += len(bloque)
            bloque.clear()

        bloque += cabecera
        numero = 0
        while True:
            linea = (
                f"Línea {numero:09d}: texto de relleno con ñandú, acción y pingüino.\n"
            ).encode("utf-8")
            if escritos + len(bloque) + len(linea) > tamano:
                break
            bloque += linea
            numero += 1
            if len(bloque) >= BLOQUE:
                volcar()
        resto = tamano - escritos - len(bloque)
        # Relleno ASCII que termina en salto de línea: nunca parte un carácter multibyte.
        if resto == 1:
            bloque += b"\n"
        elif resto > 1:
            bloque += b"x" * (resto - 1) + b"\n"
        volcar()


@dataclass(frozen=True)
class Huella:
    tamano: int
    sha256: str
    md5_base64: str


def _huella_de_bloques(bloques) -> Huella:
    sha, md5, total = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    decodificador = codecs.getincrementaldecoder("utf-8")(errors="strict")
    for bloque in bloques:
        decodificador.decode(bloque)  # lanza si no es UTF-8 válido
        sha.update(bloque)
        md5.update(bloque)
        total += len(bloque)
    decodificador.decode(b"", final=True)
    return Huella(total, sha.hexdigest(), base64.b64encode(md5.digest()).decode("ascii"))


def huella_de_archivo(ruta: Path) -> Huella:
    def bloques():
        with ruta.open("rb") as origen:
            while bloque := origen.read(BLOQUE):
                yield bloque

    return _huella_de_bloques(bloques())


# --- Operaciones contra el servidor ---------------------------------------------------------

def _descripcion(exc: BaseException) -> str:
    """Solo clase y código S3: nunca str(exc) (puede incluir endpoint o credenciales)."""
    if isinstance(exc, ClientError):
        codigo = str(exc.response.get("Error", {}).get("Code", "?"))[:40]
        return f"ClientError({codigo})"
    return type(exc).__name__


def _argumentos(bucket: str, clave: str, version: str | None) -> dict[str, str]:
    argumentos = {"Bucket": bucket, "Key": clave}
    if version:
        argumentos["VersionId"] = version
    return argumentos


def _estado_http(exc: ClientError) -> int | None:
    estado = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return estado if isinstance(estado, int) and not isinstance(estado, bool) else None


def _es_ausente(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code", "")) in _SIN_OBJETO or _estado_http(exc) == 404


def _es_rechazo_definitivo(exc: BaseException) -> bool:
    """El servidor NO guardó el objeto: 4xx (salvo 408/429) o conexión nunca establecida."""
    if isinstance(exc, _SIN_ENVIO):
        return True
    if isinstance(exc, ClientError):
        estado = _estado_http(exc)
        return estado is not None and 400 <= estado < 500 and estado not in (408, 429)
    return False


def subir(
    cliente: Any, bucket: str, clave: str, ruta: Path, huella: Huella, al_enviar: Callable[[], None] | None = None
) -> str | None:
    with ruta.open("rb") as cuerpo:
        if al_enviar is not None:
            al_enviar()  # desde aquí puede haber salido la petición
        respuesta = cliente.put_object(
            Bucket=bucket,
            Key=clave,
            Body=cuerpo,
            ContentLength=huella.tamano,
            ContentMD5=huella.md5_base64,
            ContentType=CONTENT_TYPE,
        )
    version = respuesta.get("VersionId")
    return version if isinstance(version, str) and version else None


def leer_por_bloques(cliente: Any, bucket: str, clave: str, version: str | None) -> Huella:
    cuerpo = cliente.get_object(**_argumentos(bucket, clave, version))["Body"]
    try:
        return _huella_de_bloques(cuerpo.iter_chunks(BLOQUE))
    finally:
        cuerpo.close()


def _existe(cliente: Any, bucket: str, clave: str, version: str | None) -> bool:
    try:
        cliente.head_object(**_argumentos(bucket, clave, version))
    except ClientError as exc:
        if _es_ausente(exc):
            return False
        raise
    return True


def eliminar_y_confirmar(cliente: Any, bucket: str, clave: str, version: str | None) -> bool:
    """Elimina exactamente este objeto (y esta versión) y confirma que ya no existe."""
    cliente.delete_object(**_argumentos(bucket, clave, version))
    return not _existe(cliente, bucket, clave, version)


@dataclass(frozen=True)
class ResultadoLimpieza:
    limpio: bool
    detalle: str = ""


def limpiar_remoto(
    cliente: Any, bucket: str, clave: str, *, version: str | None, estado: str, tamano: int
) -> ResultadoLimpieza:
    """Limpia SOLO `clave`. `estado`: «confirmada» (subida terminada), «rechazada» (el servidor
    no la guardó con certeza) o «incierta» (puede existir con una versión desconocida)."""
    if estado == "rechazada":
        if _existe(cliente, bucket, clave, None):
            return ResultadoLimpieza(False, "existe un objeto que esta subida no pudo crear; no se borra")
        return ResultadoLimpieza(True)
    if estado == "confirmada":
        if not eliminar_y_confirmar(cliente, bucket, clave, version):
            return ResultadoLimpieza(False, "el objeto sigue existiendo tras eliminarlo")
        if version is None:
            try:  # sin versión devuelta no hay versiones propias; se comprueba si hay permiso
                restos = listar_versiones_exactas(cliente, bucket, clave)
            except Exception:
                return ResultadoLimpieza(True)
            if restos:
                return ResultadoLimpieza(False, "quedan versiones o marcas de borrado de la clave")
        return ResultadoLimpieza(True)

    # Incierta: un HEAD 404 no prueba nada; se reconcilia por el listado de versiones.
    try:
        versiones = listar_versiones_exactas(cliente, bucket, clave)
    except Exception as exc:
        return ResultadoLimpieza(
            False,
            f"no se pudieron listar las versiones ({_descripcion(exc)}); "
            "falta el permiso s3:ListBucketVersions o el servidor falló",
        )
    objetos = [v for v in versiones if not v.es_marca_de_borrado]
    if len(objetos) > 1 or any(v.tamano != tamano for v in objetos):
        return ResultadoLimpieza(False, "el objeto hallado no es el de la prueba (tamaño o cantidad); no se borra")
    if not objetos:
        return ResultadoLimpieza(
            False, "no se halló el objeto, pero eso no prueba que la subida no se complete después"
        )
    for hallada in versiones:
        cliente.delete_object(Bucket=bucket, Key=clave, VersionId=hallada.version_id)
    if listar_versiones_exactas(cliente, bucket, clave):
        return ResultadoLimpieza(False, "el listado de versiones no quedó vacío tras eliminar")
    return ResultadoLimpieza(True)


# --- Orquestación ---------------------------------------------------------------------------------

@dataclass
class Informe:
    clave: str
    version: str | None = None
    subida_verificada: bool = False
    estado_subida: str | None = None  # None: no se intentó; «confirmada»; «rechazada»; «incierta»
    limpieza_remota_necesaria: bool = False
    limpieza_remota_confirmada: bool = False
    limpieza_local_confirmada: bool = False
    ruta_local: str = ""
    fallos: list[str] = field(default_factory=list)
    pendientes: list[str] = field(default_factory=list)

    @property
    def codigo_salida(self) -> int:
        if self.pendientes:  # la limpieza pendiente prevalece sobre el fallo de la prueba
            return EXIT_LIMPIEZA_PENDIENTE
        return EXIT_FALLO if self.fallos else EXIT_OK


def ejecutar_prueba(
    cliente: Any,
    bucket: str,
    *,
    tamano: int = TAMANO_OBJETIVO,
    directorio_temporal: str | None = None,
    imprimir: Callable[[str], None] = print,
) -> Informe:
    clave = f"{PREFIJO}/{uuid.uuid4()}/{NOMBRE_OBJETO}"
    informe = Informe(clave=clave)
    descriptor, nombre = tempfile.mkstemp(suffix=".md", prefix="igualab_minio_", dir=directorio_temporal)
    os.close(descriptor)
    ruta = Path(nombre)
    informe.ruta_local = str(ruta)
    huella: Huella | None = None
    try:
        try:
            imprimir(f"[1/5] Generando {tamano} bytes de Markdown en un archivo temporal local...")
            generar_markdown(ruta, tamano)
            huella = huella_de_archivo(ruta)
            if huella.tamano != tamano:
                raise RuntimeError(f"el archivo local mide {huella.tamano} bytes, no {tamano}")
            imprimir(f"      tamaño local: {huella.tamano} bytes | SHA-256: {huella.sha256}")

            imprimir(f"[2/5] Subiendo a {clave} ...")
            def marcar_incierta() -> None:  # desde que sale la petición, y hasta saber más, es incierta
                informe.estado_subida = "incierta"

            try:
                informe.version = subir(cliente, bucket, clave, ruta, huella, marcar_incierta)
            except Exception as exc:
                if _es_rechazo_definitivo(exc):
                    informe.estado_subida = "rechazada"
                raise
            informe.estado_subida = "confirmada"
            imprimir(f"      subido (versión: {informe.version or 'ninguna devuelta'}).")

            imprimir("[3/5] Leyendo de vuelta por bloques y comparando...")
            remoto = leer_por_bloques(cliente, bucket, clave, informe.version)
            if remoto.tamano != huella.tamano or remoto.sha256 != huella.sha256:
                informe.fallos.append(
                    f"el objeto leído no coincide (tamaño {remoto.tamano} bytes; SHA-256 {remoto.sha256})"
                )
            else:
                informe.subida_verificada = True
                imprimir("      tamaño y SHA-256 coinciden.")
        except KeyboardInterrupt:
            # Ctrl+C: se conserva el Informe, se limpia en `finally` y se muestra siempre el resumen.
            informe.fallos.append("prueba interrumpida por el usuario (Ctrl+C)")
        except Exception as exc:
            informe.fallos.append(f"error durante la prueba: {_descripcion(exc)}")
    finally:
        # Se intenta siempre que pudo salir una petición, incluso ante Ctrl+C.
        if informe.estado_subida is not None:
            informe.limpieza_remota_necesaria = True
            imprimir(f"[4/5] Limpiando SOLO el objeto de esta prueba (subida {informe.estado_subida})...")
            try:
                resultado = limpiar_remoto(
                    cliente, bucket, clave, version=informe.version, estado=informe.estado_subida,
                    tamano=huella.tamano if huella else tamano,
                )
            except (Exception, KeyboardInterrupt) as exc:  # un segundo Ctrl+C tampoco pierde el informe
                resultado = ResultadoLimpieza(False, f"error al limpiar: {_descripcion(exc)}")
            informe.limpieza_remota_confirmada = resultado.limpio
            if resultado.limpio:
                imprimir("      limpieza remota confirmada.")
            else:
                imprimir(f"      limpieza remota NO confirmada: {resultado.detalle}")
                informe.pendientes.append(
                    "LIMPIEZA REMOTA PENDIENTE: no se puede dar por limpio el objeto "
                    f"{clave}" + (f" (versión {informe.version})" if informe.version else "")
                    + f" — {resultado.detalle}. Compruebe a mano TODAS las versiones y marcas de borrado "
                    "de esa clave exacta y elimínelas por su versión; no toque otras claves."
                )
        imprimir("[5/5] Borrando el archivo local...")
        try:
            ruta.unlink(missing_ok=True)
            informe.limpieza_local_confirmada = not ruta.exists()
        except OSError as exc:
            imprimir(f"      error al borrar el archivo local: {_descripcion(exc)}")
        if not informe.limpieza_local_confirmada:
            informe.pendientes.append(f"LIMPIEZA LOCAL PENDIENTE: borre a mano {ruta}")
    return informe


def _resumen(informe: Informe, imprimir: Callable[[str], None]) -> None:
    imprimir("")
    imprimir("=== Resultado ===")
    imprimir(f"Subida y lectura verificadas (tamaño + SHA-256): {'SÍ' if informe.subida_verificada else 'NO'}")
    if informe.limpieza_remota_necesaria:
        imprimir(f"Limpieza remota confirmada:                      {'SÍ' if informe.limpieza_remota_confirmada else 'NO'}")
    imprimir(f"Limpieza local confirmada:                       {'SÍ' if informe.limpieza_local_confirmada else 'NO'}")
    for falla in informe.fallos:
        imprimir(f"FALLO: {falla}")
    for pendiente in informe.pendientes:
        imprimir(pendiente)
    imprimir(f"Código de salida: {informe.codigo_salida} (0 limpio y verificado; 1 falló y quedó limpio; 2 limpieza pendiente).")
    imprimir("Esta prueba valida MinIO, no el endpoint multipart, el proxy ni el respaldo.")


def _argumentos_cli(argv: list[str] | None) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(
        description="Prueba manual de MinIO con 50 000 000 bytes (ver docs/ingesta/07-almacenamiento-minio.md)."
    )
    analizador.add_argument("--ejecutar", action="store_true", help="Confirma que quiere ejecutar la prueba remota.")
    analizador.add_argument("--endpoint", help="URL https de MinIO (o MINIO_ENDPOINT_URL).")
    analizador.add_argument("--bucket", help="Bucket existente (o MINIO_BUCKET). No se crea.")
    analizador.add_argument("--region", help="Región, si se conoce (o MINIO_REGION).")
    return analizador.parse_args(argv)


def main(
    argv: list[str] | None = None,
    entorno: Mapping[str, str] | None = None,
    crear_cliente: Callable[[ConfigAlmacenamiento], Any] = crear_cliente_s3,
    pedir_secreto: Callable[[str], str] = getpass.getpass,
    imprimir: Callable[[str], None] = print,
) -> int:
    argumentos = _argumentos_cli(argv)
    entorno = os.environ if entorno is None else entorno
    if not argumentos.ejecutar:
        imprimir("Prueba remota NO ejecutada. Use --ejecutar para confirmar (ver docs/ingesta/07-almacenamiento-minio.md).")
        return EXIT_FALLO
    endpoint = argumentos.endpoint or entorno.get("MINIO_ENDPOINT_URL")
    bucket = argumentos.bucket or entorno.get("MINIO_BUCKET")
    region = argumentos.region or entorno.get("MINIO_REGION") or None
    if not endpoint or not bucket:
        imprimir("Faltan --endpoint/MINIO_ENDPOINT_URL o --bucket/MINIO_BUCKET.")
        return EXIT_FALLO
    acceso = entorno.get("MINIO_ACCESS_KEY") or pedir_secreto("Clave de acceso (no se muestra): ")
    secreto = entorno.get("MINIO_SECRET_KEY") or pedir_secreto("Clave secreta (no se muestra): ")
    try:
        config = ConfigAlmacenamiento(
            endpoint_url=endpoint,
            bucket=bucket,
            ambiente=AMBIENTE,
            access_key=acceso,
            secret_key=secreto,
            region=region,
            connect_timeout=CONNECT_TIMEOUT_SEGUNDOS,
            read_timeout=READ_TIMEOUT_SEGUNDOS,
            # Obligatorio en la configuración, pero este script NO lo aplica (SDK síncrono, sin plazo total).
            operation_timeout=READ_TIMEOUT_SEGUNDOS,
        )
    except ValueError as exc:
        imprimir(f"Configuración inválida: {exc}")  # los mensajes de ValueError no repiten valores
        return EXIT_FALLO
    imprimir(
        f"Plazos efectivos: conexión {CONNECT_TIMEOUT_SEGUNDOS} s; lectura de socket {READ_TIMEOUT_SEGUNDOS} s sin "
        "recibir bytes. NO hay plazo total por operación; sin reintentos (una petición por operación)."
    )
    cliente = crear_cliente(config)
    try:
        informe = ejecutar_prueba(cliente, bucket, imprimir=imprimir)
    finally:
        cerrar = getattr(cliente, "close", None)
        if callable(cerrar):
            try:
                cerrar()
            except Exception as exc:
                imprimir(f"No se pudo cerrar el cliente S3: {_descripcion(exc)}")
    _resumen(informe, imprimir)
    return informe.codigo_salida


if __name__ == "__main__":
    sys.exit(main())
