"""PRUEBA MANUAL de MinIO con un Markdown de exactamente 50 000 000 bytes.

NO forma parte de pytest ni del arranque: hay que ejecutarla de forma explícita
(`--ejecutar`). Valida MinIO (subida, lectura por bloques, tamaño, SHA-256, eliminación);
NO valida la aceptación de 50 MB por el endpoint multipart (aún no existe el router).

Qué hace:
 1. Genera en un archivo temporal local un Markdown UTF-8 válido de EXACTAMENTE
    50 000 000 bytes, por bloques (no lo carga entero en memoria).
 2. Calcula su tamaño y SHA-256 leyéndolo por bloques.
 3. Lo sube a `development/pruebas/<UUID>/prueba_50000000_bytes.md` del bucket indicado.
 4. Lo vuelve a leer del servidor por bloques y compara tamaño y SHA-256.
 5. Elimina SOLO ese objeto (y, si el bucket tiene versionado, SOLO la versión creada
    por esta prueba) y confirma que ya no existe.
 6. Borra el archivo local. Informa POR SEPARADO cualquier limpieza pendiente, remota o
    local, con los datos necesarios para hacerla a mano.

Qué NO hace: no crea buckets, no cambia permisos, no toca otros objetos, no imprime
credenciales. Las credenciales NO se aceptan como argumentos (quedarían en el
historial): se leen de las variables de entorno MINIO_ACCESS_KEY y MINIO_SECRET_KEY
de la sesión actual o, si faltan, se piden sin eco. Este script no lee archivos `.env`.

Código de salida: 0 todo verificado y limpio; 1 fallo de la prueba; 2 limpieza pendiente.

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

from botocore.exceptions import ClientError

from app.services.ingesta.almacenamiento import ConfigAlmacenamiento
from app.services.ingesta.almacenamiento_s3 import crear_cliente_s3

TAMANO_OBJETIVO = 50_000_000
AMBIENTE = "development"
PREFIJO = f"{AMBIENTE}/pruebas"
NOMBRE_OBJETO = "prueba_50000000_bytes.md"
BLOQUE = 1024 * 1024
CONTENT_TYPE = "text/markdown; charset=utf-8"

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


def subir(cliente: Any, bucket: str, clave: str, ruta: Path, huella: Huella) -> str | None:
    with ruta.open("rb") as cuerpo:
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


def eliminar_y_confirmar(cliente: Any, bucket: str, clave: str, version: str | None) -> bool:
    """Elimina exactamente este objeto (y esta versión) y confirma que ya no existe."""
    cliente.delete_object(**_argumentos(bucket, clave, version))
    try:
        cliente.head_object(**_argumentos(bucket, clave, version))
    except ClientError as exc:
        codigo = str(exc.response.get("Error", {}).get("Code", ""))
        if codigo in {"404", "NoSuchKey", "NotFound", "NoSuchVersion"} or (
            exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
        ):
            return True
        raise
    return False


# --- Orquestación ---------------------------------------------------------------------------------

@dataclass
class Informe:
    clave: str
    version: str | None = None
    subida_verificada: bool = False
    eliminacion_remota_confirmada: bool = False
    objeto_remoto_creado: bool = False
    limpieza_local_confirmada: bool = False
    ruta_local: str = ""
    fallos: list[str] = field(default_factory=list)
    pendientes: list[str] = field(default_factory=list)

    @property
    def codigo_salida(self) -> int:
        if self.fallos:
            return EXIT_FALLO
        return EXIT_LIMPIEZA_PENDIENTE if self.pendientes else EXIT_OK


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
    try:
        try:
            imprimir(f"[1/5] Generando {tamano} bytes de Markdown en un archivo temporal local...")
            generar_markdown(ruta, tamano)
            huella = huella_de_archivo(ruta)
            if huella.tamano != tamano:
                raise RuntimeError(f"el archivo local mide {huella.tamano} bytes, no {tamano}")
            imprimir(f"      tamaño local: {huella.tamano} bytes | SHA-256: {huella.sha256}")

            imprimir(f"[2/5] Subiendo a {clave} ...")
            informe.objeto_remoto_creado = True  # a partir de aquí puede existir en el servidor
            informe.version = subir(cliente, bucket, clave, ruta, huella)
            imprimir(f"      subido (versión: {informe.version or 'sin versionado'}).")

            imprimir("[3/5] Leyendo de vuelta por bloques y comparando...")
            remoto = leer_por_bloques(cliente, bucket, clave, informe.version)
            if remoto.tamano != huella.tamano or remoto.sha256 != huella.sha256:
                informe.fallos.append(
                    f"el objeto leído no coincide (tamaño {remoto.tamano} bytes; SHA-256 {remoto.sha256})"
                )
            else:
                informe.subida_verificada = True
                imprimir("      tamaño y SHA-256 coinciden.")
        except Exception as exc:
            informe.fallos.append(f"error durante la prueba: {_descripcion(exc)}")
    finally:
        # La limpieza remota se intenta siempre que pudo crearse el objeto.
        if informe.objeto_remoto_creado:
            imprimir("[4/5] Eliminando SOLO el objeto de esta prueba y confirmando...")
            try:
                informe.eliminacion_remota_confirmada = eliminar_y_confirmar(
                    cliente, bucket, clave, informe.version
                )
            except Exception as exc:
                imprimir(f"      error al eliminar: {_descripcion(exc)}")
            if informe.eliminacion_remota_confirmada:
                imprimir("      eliminación confirmada.")
            else:
                informe.pendientes.append(
                    "LIMPIEZA REMOTA PENDIENTE: elimine a mano el objeto "
                    f"{clave}" + (f" (versión {informe.version})" if informe.version else "")
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
    if informe.objeto_remoto_creado:
        imprimir(f"Eliminación remota confirmada:                   {'SÍ' if informe.eliminacion_remota_confirmada else 'NO'}")
    imprimir(f"Limpieza local confirmada:                       {'SÍ' if informe.limpieza_local_confirmada else 'NO'}")
    for falla in informe.fallos:
        imprimir(f"FALLO: {falla}")
    for pendiente in informe.pendientes:
        imprimir(pendiente)
    imprimir("Esta prueba valida MinIO, no el endpoint multipart ni el respaldo.")


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
            connect_timeout=10,
            read_timeout=120,
            operation_timeout=600,
        )
    except ValueError as exc:
        imprimir(f"Configuración inválida: {exc}")  # los mensajes de ValueError no repiten valores
        return EXIT_FALLO
    informe = ejecutar_prueba(crear_cliente(config), bucket, imprimir=imprimir)
    _resumen(informe, imprimir)
    return informe.codigo_salida


if __name__ == "__main__":
    sys.exit(main())
