"""Pruebas del script MANUAL `scripts/pruebas_manuales/minio_50mb.py`.

Solo prueban su lógica con un cliente S3 falso y archivos temporales: NO ejecutan la
prueba remota ni leen credenciales reales. El script en sí no se recolecta en pytest.
"""
import os
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from scripts.pruebas_manuales import minio_50mb as script
from tests.services.test_ingesta_almacenamiento import ClienteS3Falso, error_cliente

ACCESO = "ACCESO-SOLO-PARA-PRUEBA"
SECRETO = "SECRETO-SOLO-PARA-PRUEBA"
ENTORNO = {
    "MINIO_ENDPOINT_URL": "https://almacen.ejemplo.invalid",
    "MINIO_BUCKET": "bucket-prueba",
    "MINIO_ACCESS_KEY": ACCESO,
    "MINIO_SECRET_KEY": SECRETO,
}


class ClienteConBloque(ClienteS3Falso):
    """Cliente falso cuyo cuerpo admite `iter_chunks`, como el StreamingBody de botocore."""

    def put_object(self, **kw):
        cuerpo = kw["Body"]
        kw = {**kw, "Body": cuerpo.read()}  # el script pasa un archivo abierto
        return super().put_object(**kw)

    def get_object(self, **kw):
        respuesta = super().get_object(**kw)
        datos = respuesta["Body"].read()
        cuerpo = respuesta["Body"]
        cuerpo.iter_chunks = lambda tamano: (datos[i:i + tamano] for i in range(0, len(datos), tamano))
        return respuesta


def ejecutar(cliente, tmp_path, tamano=1_000_003, imprimir=None):
    salida: list[str] = []
    informe = script.ejecutar_prueba(
        cliente, "bucket-prueba", tamano=tamano, directorio_temporal=str(tmp_path),
        imprimir=imprimir or salida.append,
    )
    return informe, salida


# --- Generación ----------------------------------------------------------------------------------

def test_el_archivo_generado_mide_exactamente_50_000_000_bytes_y_es_utf8_valido(tmp_path):
    ruta = tmp_path / "prueba.md"
    script.generar_markdown(ruta)
    assert ruta.stat().st_size == script.TAMANO_OBJETIVO == 50_000_000
    huella = script.huella_de_archivo(ruta)  # lectura por bloques con decodificador UTF-8 estricto
    assert huella.tamano == 50_000_000
    with ruta.open("rb") as archivo:
        assert archivo.read(30).startswith("# Prueba de almacenamiento\n".encode())
    with ruta.open("rb") as archivo:
        archivo.seek(-200, os.SEEK_END)
        assert archivo.read().endswith(b"\n")


@pytest.mark.parametrize("tamano", [2, 3, 10, 40, 1_000, 1_048_575, 1_048_576, 1_048_577, 3_000_001])
def test_el_tamano_exacto_se_cumple_para_cualquier_valor_y_sin_partir_caracteres(tmp_path, tamano):
    ruta = tmp_path / "p.md"
    script.generar_markdown(ruta, tamano)
    assert ruta.stat().st_size == tamano
    ruta.read_bytes().decode("utf-8")  # no lanza


@pytest.mark.parametrize("tamano", [0, 1, -1, True, 2.5, "10"])
def test_tamano_invalido(tmp_path, tamano):
    with pytest.raises(ValueError):
        script.generar_markdown(tmp_path / "p.md", tamano)


def test_la_huella_rechaza_utf8_invalido_y_coincide_con_hashlib(tmp_path):
    import hashlib

    bueno = tmp_path / "b.md"
    bueno.write_bytes("ñandú\n".encode() * 100_000)
    huella = script.huella_de_archivo(bueno)
    assert huella.sha256 == hashlib.sha256(bueno.read_bytes()).hexdigest()
    malo = tmp_path / "m.md"
    malo.write_bytes(b"ok \xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        script.huella_de_archivo(malo)


# --- Flujo completo con cliente falso ------------------------------------------------------------------

def test_flujo_completo_sube_verifica_elimina_solo_su_objeto_y_limpia_el_archivo_local(tmp_path):
    cliente = ClienteConBloque()
    cliente.objetos[("development/otros/dato.md", None)] = b"ajeno"
    cliente.objetos[("qa/documentos/x/original.md", None)] = b"ajeno qa"
    informe, salida = ejecutar(cliente, tmp_path)

    assert informe.codigo_salida == script.EXIT_OK
    assert informe.subida_verificada and informe.eliminacion_remota_confirmada and informe.limpieza_local_confirmada
    assert informe.clave.startswith("development/pruebas/") and informe.clave.endswith("/prueba_50000000_bytes.md")
    assert len(informe.clave.split("/")[2]) == 36  # UUID
    # Solo se tocó el objeto propio; los ajenos siguen y el propio ya no existe.
    assert set(cliente.objetos) == {("development/otros/dato.md", None), ("qa/documentos/x/original.md", None)}
    claves_usadas = {kw["Key"] for _, kw in cliente.llamadas}
    assert claves_usadas == {informe.clave}
    assert not Path(informe.ruta_local).exists() and not list(tmp_path.iterdir())
    metodos = cliente.metodos()
    assert metodos == ["put_object", "get_object", "delete_object", "head_object"]
    assert "create_bucket" not in metodos and not hasattr(cliente, "create_bucket")


def test_con_versionado_se_elimina_la_version_creada_por_la_prueba(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.objetos[("development/otros/dato.md", "v-previa")] = b"previa"
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.version == "v1" and informe.codigo_salida == script.EXIT_OK
    borrado = [kw for m, kw in cliente.llamadas if m == "delete_object"][0]
    assert borrado["VersionId"] == "v1" and borrado["Key"] == informe.clave
    # También se lee y se confirma por versión.
    assert all(kw.get("VersionId") == "v1" for m, kw in cliente.llamadas if m in {"get_object", "head_object"})
    assert ("development/otros/dato.md", "v-previa") in cliente.objetos


def test_si_el_contenido_leido_no_coincide_la_prueba_falla_pero_igual_limpia(tmp_path):
    cliente = ClienteConBloque()
    original = cliente.put_object

    def corrompe(**kw):
        resultado = original(**kw)
        clave = (kw["Key"], None)
        cliente.objetos[clave] = cliente.objetos[clave][:-1] + b"X"
        return resultado

    cliente.put_object = corrompe
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_FALLO
    assert not informe.subida_verificada and any("no coincide" in f for f in informe.fallos)
    assert informe.eliminacion_remota_confirmada and informe.limpieza_local_confirmada


def test_si_falla_la_eliminacion_remota_se_informa_pendiente_con_la_clave_y_no_se_declara_limpio(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.errores["delete_object"] = error_cliente("AccessDenied", 403, f"detalle con {SECRETO}")
    informe, salida = ejecutar(cliente, tmp_path)

    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE
    assert informe.subida_verificada and not informe.eliminacion_remota_confirmada
    assert informe.limpieza_local_confirmada  # se informa por separado
    [pendiente] = informe.pendientes
    assert "LIMPIEZA REMOTA PENDIENTE" in pendiente and informe.clave in pendiente and "v1" in pendiente
    assert SECRETO not in "\n".join(salida)


def test_si_el_objeto_sigue_existiendo_tras_eliminar_no_se_confirma(tmp_path):
    cliente = ClienteConBloque()
    cliente.delete_object = lambda **kw: cliente.llamadas.append(("delete_object", kw)) or {}
    informe, _ = ejecutar(cliente, tmp_path)
    assert not informe.eliminacion_remota_confirmada
    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE


def test_un_fallo_de_subida_se_informa_e_intenta_limpiar_lo_que_pudo_crearse(tmp_path):
    cliente = ClienteConBloque()
    cliente.errores["put_object"] = ConnectionError(f"sin red hacia {SECRETO}")
    informe, salida = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_FALLO and not informe.subida_verificada
    assert informe.eliminacion_remota_confirmada  # el objeto no existía: la eliminación se confirma
    assert "ConnectionError" in "\n".join(informe.fallos) and SECRETO not in "\n".join(salida + informe.fallos)
    assert not list(tmp_path.iterdir())


def test_si_no_se_pudo_borrar_el_archivo_local_se_informa_por_separado(tmp_path, monkeypatch):
    cliente = ClienteConBloque()
    original = Path.unlink

    def no_borra(self, *a, **k):
        raise PermissionError(f"bloqueado {SECRETO}")

    monkeypatch.setattr(Path, "unlink", no_borra)
    informe, salida = ejecutar(cliente, tmp_path)
    monkeypatch.setattr(Path, "unlink", original)

    assert informe.eliminacion_remota_confirmada and not informe.limpieza_local_confirmada
    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE
    assert any("LIMPIEZA LOCAL PENDIENTE" in p and informe.ruta_local in p for p in informe.pendientes)
    assert SECRETO not in "\n".join(salida)
    Path(informe.ruta_local).unlink()  # limpieza de la propia prueba


def test_si_la_generacion_local_falla_no_se_crea_nada_remoto(tmp_path):
    cliente = ClienteConBloque()
    informe, _ = ejecutar(cliente, tmp_path, tamano=1)  # tamaño inválido
    assert informe.codigo_salida == script.EXIT_FALLO and cliente.llamadas == []
    assert not informe.objeto_remoto_creado and informe.limpieza_local_confirmada


# --- main: ejecución explícita y credenciales privadas ---------------------------------------------------

def principal(argv, entorno, cliente=None, secretos=None):
    salida: list[str] = []
    pedidos: list[str] = []

    def pedir(mensaje):
        pedidos.append(mensaje)
        return (secretos or {}).get(len(pedidos), "")

    creados = []

    def crear(config):
        creados.append(config)
        return cliente or ClienteConBloque()

    codigo = script.main(argv, entorno, crear_cliente=crear, pedir_secreto=pedir, imprimir=salida.append)
    return codigo, salida, pedidos, creados


def test_sin_ejecutar_no_hace_nada_y_no_crea_cliente_ni_pide_credenciales():
    codigo, salida, pedidos, creados = principal([], ENTORNO)
    assert codigo == script.EXIT_FALLO and creados == [] and pedidos == []
    assert any("NO ejecutada" in linea for linea in salida)


def test_faltan_endpoint_o_bucket():
    codigo, salida, _, creados = principal(["--ejecutar"], {"MINIO_ACCESS_KEY": ACCESO, "MINIO_SECRET_KEY": SECRETO})
    assert codigo == script.EXIT_FALLO and creados == []


def test_las_credenciales_se_leen_del_entorno_y_nunca_se_imprimen(tmp_path, monkeypatch):
    monkeypatch.setattr(script.tempfile, "tempdir", str(tmp_path))
    codigo, salida, pedidos, creados = principal(["--ejecutar"], ENTORNO)
    assert codigo == script.EXIT_OK and pedidos == []
    [config] = creados
    assert (config.access_key, config.secret_key, config.ambiente) == (ACCESO, SECRETO, "development")
    assert config.endpoint_url == ENTORNO["MINIO_ENDPOINT_URL"] and config.bucket == "bucket-prueba"
    texto = "\n".join(salida)
    assert ACCESO not in texto and SECRETO not in texto and "ejemplo.invalid" not in texto
    assert ACCESO not in repr(config) and SECRETO not in repr(config)


def test_sin_credenciales_en_el_entorno_se_piden_sin_eco(tmp_path, monkeypatch):
    monkeypatch.setattr(script.tempfile, "tempdir", str(tmp_path))
    entorno = {k: v for k, v in ENTORNO.items() if not k.endswith("_KEY")}
    codigo, salida, pedidos, creados = principal(["--ejecutar"], entorno, secretos={1: ACCESO, 2: SECRETO})
    assert codigo == script.EXIT_OK and len(pedidos) == 2
    assert (creados[0].access_key, creados[0].secret_key) == (ACCESO, SECRETO)
    assert ACCESO not in "\n".join(salida + pedidos) and SECRETO not in "\n".join(salida + pedidos)


def test_no_acepta_credenciales_como_argumentos():
    with pytest.raises(SystemExit):
        principal(["--ejecutar", "--access-key", "x", "--secret-key", "y"], ENTORNO)


def test_endpoint_http_o_con_credenciales_se_rechaza_sin_repetir_el_valor():
    entorno = {**ENTORNO, "MINIO_ENDPOINT_URL": "http://usuario:clave-privada@almacen.invalid"}
    codigo, salida, _, creados = principal(["--ejecutar"], entorno)
    assert codigo == script.EXIT_FALLO and creados == []
    assert "clave-privada" not in "\n".join(salida)


def test_los_argumentos_tienen_prioridad_sobre_el_entorno_y_la_region_es_opcional(tmp_path, monkeypatch):
    monkeypatch.setattr(script.tempfile, "tempdir", str(tmp_path))
    _, _, _, creados = principal(
        ["--ejecutar", "--endpoint", "https://otro.ejemplo.invalid", "--bucket", "otro-bucket", "--region", "us-east-1"],
        ENTORNO,
    )
    assert (creados[0].endpoint_url, creados[0].bucket, creados[0].region) == (
        "https://otro.ejemplo.invalid", "otro-bucket", "us-east-1")
    _, _, _, sin_region = principal(["--ejecutar"], ENTORNO)
    assert sin_region[0].region is None


def test_el_script_no_esta_en_la_recoleccion_de_pytest():
    assert not Path(script.__file__).name.startswith("test_")
