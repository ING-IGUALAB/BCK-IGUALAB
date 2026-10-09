"""Pruebas del script MANUAL `scripts/pruebas_manuales/minio_50mb.py`.

Solo prueban su lógica con un cliente S3 falso y archivos temporales: NO ejecutan la
prueba remota ni leen credenciales reales. El script en sí no se recolecta en pytest.
"""
import hashlib
import os
from pathlib import Path

import pytest
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from scripts.pruebas_manuales import minio_50mb as script
from tests.ayudantes_ingesta import ClienteS3Versionado, error_cliente

ACCESO = "ACCESO-SOLO-PARA-PRUEBA"
SECRETO = "SECRETO-SOLO-PARA-PRUEBA"
ENTORNO = {
    "MINIO_ENDPOINT_URL": "https://almacen.ejemplo.invalid",
    "MINIO_BUCKET": "bucket-prueba",
    "MINIO_ACCESS_KEY": ACCESO,
    "MINIO_SECRET_KEY": SECRETO,
}
TAMANO = 1_000_003


class ClienteConBloque(ClienteS3Versionado):
    """Cliente falso cuyo cuerpo admite `iter_chunks`/`close`, como el StreamingBody de botocore,
    y que lee el archivo abierto que le pasa el script."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cerrado = 0

    def put_object(self, **kw):
        return super().put_object(**{**kw, "Body": kw["Body"].read()})

    def get_object(self, **kw):
        respuesta = super().get_object(**kw)
        datos = respuesta["Body"].read()
        respuesta["Body"].iter_chunks = lambda tamano: (datos[i:i + tamano] for i in range(0, len(datos), tamano))
        return respuesta

    def close(self):
        self.cerrado += 1


def ejecutar(cliente, tmp_path, tamano=TAMANO, imprimir=None):
    salida: list[str] = []
    informe = script.ejecutar_prueba(
        cliente, "bucket-prueba", tamano=tamano, directorio_temporal=str(tmp_path),
        imprimir=imprimir or salida.append,
    )
    return informe, salida


def borrados(cliente) -> list[dict]:
    return [kw for metodo, kw in cliente.llamadas if metodo == "delete_object"]


def lectura_ajena(cliente, clave):
    """Todas las llamadas deben referirse solo a la clave de la prueba."""
    return {kw["Key"] for m, kw in cliente.llamadas if "Key" in kw} | {
        kw["Prefix"] for m, kw in cliente.llamadas if "Prefix" in kw
    } == {clave}


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
    bueno = tmp_path / "b.md"
    bueno.write_bytes("ñandú\n".encode() * 100_000)
    huella = script.huella_de_archivo(bueno)
    assert huella.sha256 == hashlib.sha256(bueno.read_bytes()).hexdigest()
    malo = tmp_path / "m.md"
    malo.write_bytes(b"ok \xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        script.huella_de_archivo(malo)


# --- Flujo completo y versionado -----------------------------------------------------------------------

def test_con_versionado_la_subida_confirmada_elimina_y_verifica_solo_esa_version(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    previa = cliente.poner("development/otros/dato.md", b"previa")
    ajeno_qa = cliente.poner("qa/documentos/x/original.md", b"ajeno qa")
    informe, _ = ejecutar(cliente, tmp_path)

    assert informe.codigo_salida == script.EXIT_OK
    assert informe.subida_verificada and informe.limpieza_remota_confirmada and informe.limpieza_local_confirmada
    assert informe.clave.startswith("development/pruebas/") and informe.clave.endswith("/prueba_50000000_bytes.md")
    assert len(informe.clave.split("/")[2]) == 36  # UUID
    assert informe.version is not None
    assert [kw["VersionId"] for kw in borrados(cliente)] == [informe.version]  # esa versión, sin marca de borrado
    assert cliente.versiones_de(informe.clave) == []
    assert [e["VersionId"] for e in cliente.versiones_de("development/otros/dato.md")] == [previa]  # ajenos intactos
    assert [e["VersionId"] for e in cliente.versiones_de("qa/documentos/x/original.md")] == [ajeno_qa]
    assert all(kw.get("VersionId") == informe.version for m, kw in cliente.llamadas if m in {"get_object", "head_object"})
    assert lectura_ajena(cliente, informe.clave)
    assert not Path(informe.ruta_local).exists() and not list(tmp_path.iterdir())
    assert not hasattr(cliente, "create_bucket")


def test_sin_versionado_se_elimina_la_clave_y_se_comprueba_que_no_queda_nada(tmp_path):
    cliente = ClienteConBloque(versionado=False)
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_OK and informe.version is None
    assert borrados(cliente) == [{"Bucket": "bucket-prueba", "Key": informe.clave}]
    assert cliente.versiones_de(informe.clave) == []


def test_sin_versionado_y_sin_permiso_de_listado_basta_el_head_porque_no_hay_version_devuelta(tmp_path):
    cliente = ClienteConBloque(versionado=False, permitir_listado=False)
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_OK


def test_si_tras_eliminar_la_clave_sin_version_quedan_restos_se_informa_pendiente(tmp_path):
    cliente = ClienteConBloque(versionado=False)
    original = cliente.delete_object

    def deja_resto(**kw):
        original(**kw)
        cliente.entradas.append({"Key": kw["Key"], "VersionId": "resto", "Body": b"resto", "Marca": False})
        return {}

    cliente.delete_object = deja_resto
    cliente.head_object = lambda **kw: (_ for _ in ()).throw(error_cliente("404", 404))  # HEAD «prueba» ausencia
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE and "quedan versiones" in informe.pendientes[0]


# --- Resultado incierto ------------------------------------------------------------------------------------------

def test_resultado_incierto_con_el_objeto_creado_se_reconcilia_listando_y_borrando_por_version(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.error_tras_crear = ReadTimeoutError(endpoint_url="https://x.invalid")  # el servidor guardó
    informe, _ = ejecutar(cliente, tmp_path)

    assert informe.estado_subida == "incierta" and informe.version is None  # la versión se desconoce
    assert informe.codigo_salida == script.EXIT_FALLO  # la prueba falló, pero quedó limpio
    assert informe.limpieza_remota_confirmada and informe.pendientes == []
    assert cliente.versiones_de(informe.clave) == []
    assert all("VersionId" in kw for kw in borrados(cliente))  # nunca DELETE sin versión
    assert "head_object" not in cliente.metodos()  # un HEAD 404 no habría probado nada


def test_resultado_incierto_con_marca_de_borrado_encima_elimina_version_y_marca(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.error_tras_crear = ReadTimeoutError(endpoint_url="https://x.invalid")
    cliente.delete_object(Bucket="b", Key="otra")  # irrelevante: marca en otra clave
    original = cliente.put_object

    def crea_y_oculta(**kw):
        try:
            return original(**kw)
        finally:  # alguien dejó una marca de borrado sobre la versión recién creada
            cliente.delete_object(Bucket="b", Key=kw["Key"])
            cliente.llamadas.pop()

    cliente.put_object = crea_y_oculta
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.limpieza_remota_confirmada and cliente.versiones_de(informe.clave) == []


def test_resultado_incierto_sin_permiso_de_listado_deja_la_limpieza_pendiente_con_codigo_2_aunque_la_prueba_fallo(tmp_path):
    cliente = ClienteConBloque(versionado=True, permitir_listado=False)
    cliente.error_tras_crear = ReadTimeoutError(endpoint_url="https://x.invalid")
    informe, salida = ejecutar(cliente, tmp_path)

    assert informe.fallos  # la prueba falló...
    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE  # ...pero lo pendiente prevalece
    [pendiente] = informe.pendientes
    assert "LIMPIEZA REMOTA PENDIENTE" in pendiente and informe.clave in pendiente
    assert "s3:ListBucketVersions" in pendiente
    assert borrados(cliente) == [] and len(cliente.versiones_de(informe.clave)) == 1  # no se borró a ciegas


def test_resultado_incierto_sin_hallazgos_no_se_da_por_limpio(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.errores["put_object"] = ReadTimeoutError(endpoint_url="https://x.invalid")  # antes de crear nada
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE
    assert "no prueba" in informe.pendientes[0]
    assert "head_object" not in cliente.metodos() and borrados(cliente) == []


def test_un_objeto_hallado_de_otro_tamano_o_mas_de_uno_no_se_borra(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.errores["put_object"] = ReadTimeoutError(endpoint_url="https://x.invalid")
    original = cliente.put_object

    def aparece_algo(**kw):
        cliente.poner(kw["Key"], b"otro tamano")  # algo ajeno aparece en esa clave
        return original(**kw)

    cliente.put_object = aparece_algo
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE and "no es el de la prueba" in informe.pendientes[0]
    assert borrados(cliente) == [] and len(cliente.versiones_de(informe.clave)) == 1


def test_el_listado_nunca_toca_claves_vecinas(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.error_tras_crear = ReadTimeoutError(endpoint_url="https://x.invalid")
    vecina = cliente.poner("development/pruebas/", b"vecina")  # el prefijo no es la clave
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.limpieza_remota_confirmada
    assert [e["VersionId"] for e in cliente.versiones_de("development/pruebas/")] == [vecina]


def test_un_rechazo_definitivo_no_necesita_borrar_nada_y_queda_limpio(tmp_path):
    for error in (error_cliente("AccessDenied", 403), ConnectTimeoutError(endpoint_url="https://x.invalid")):
        cliente = ClienteConBloque()
        cliente.errores["put_object"] = error
        informe, _ = ejecutar(cliente, tmp_path)
        assert informe.estado_subida == "rechazada" and informe.codigo_salida == script.EXIT_FALLO
        assert informe.limpieza_remota_confirmada and borrados(cliente) == []


def test_un_objeto_inesperado_tras_un_rechazo_definitivo_queda_pendiente_sin_borrarse(tmp_path):
    cliente = ClienteConBloque()
    cliente.errores["put_object"] = error_cliente("AccessDenied", 403)
    original = cliente.put_object

    def con_objeto(**kw):
        cliente.poner(kw["Key"], b"ya habia algo")
        return original(**kw)

    cliente.put_object = con_objeto
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE and borrados(cliente) == []


def test_si_falla_el_open_local_antes_de_enviar_no_hay_nada_que_limpiar_en_remoto(tmp_path, monkeypatch):
    cliente = ClienteConBloque()
    original = Path.open

    def falla_al_leer(self, mode="r", *a, **k):
        if "b" in mode and "r" in mode and self.name.startswith("igualab_minio_"):
            raise PermissionError("bloqueado")
        return original(self, mode, *a, **k)

    monkeypatch.setattr(Path, "open", falla_al_leer)
    informe, _ = ejecutar(cliente, tmp_path)
    monkeypatch.undo()
    assert informe.fallos and cliente.llamadas == [] and informe.codigo_salida == script.EXIT_FALLO


def test_una_interrupcion_del_teclado_se_registra_limpia_y_devuelve_el_informe(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.error_tras_crear = KeyboardInterrupt()
    informe, _ = ejecutar(cliente, tmp_path)  # ya no se propaga: el informe se conserva
    assert any("interrumpida" in f for f in informe.fallos)
    assert informe.limpieza_remota_confirmada and informe.codigo_salida == script.EXIT_FALLO
    assert cliente.entradas == [] and not list(tmp_path.iterdir())


def test_un_segundo_ctrl_c_durante_la_limpieza_no_pierde_el_informe(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.error_tras_crear = KeyboardInterrupt()
    cliente.errores["list_object_versions"] = KeyboardInterrupt()
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == script.EXIT_LIMPIEZA_PENDIENTE and informe.pendientes


# --- Códigos de salida -----------------------------------------------------------------------------------------------

def test_0_si_todo_se_verifica_y_queda_limpio(tmp_path):
    informe, _ = ejecutar(ClienteConBloque(), tmp_path)
    assert informe.codigo_salida == 0


def test_1_si_falla_la_prueba_pero_queda_limpio(tmp_path):
    cliente = ClienteConBloque()
    original = cliente.put_object

    def corrompe(**kw):
        resultado = original(**kw)
        for entrada in cliente.entradas:
            entrada["Body"] = entrada["Body"][:-1] + b"X"
        return resultado

    cliente.put_object = corrompe
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == 1
    assert not informe.subida_verificada and any("no coincide" in f for f in informe.fallos)
    assert informe.limpieza_remota_confirmada and informe.limpieza_local_confirmada


def test_2_si_la_prueba_pasa_pero_la_limpieza_remota_falla(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.errores["delete_object"] = error_cliente("AccessDenied", 403, f"detalle con {SECRETO}")
    informe, salida = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == 2 and informe.subida_verificada and not informe.limpieza_remota_confirmada
    assert informe.limpieza_local_confirmada  # se informa por separado
    assert informe.version in informe.pendientes[0] and informe.clave in informe.pendientes[0]
    assert SECRETO not in "\n".join(salida + informe.pendientes)


def test_2_si_el_objeto_sigue_existiendo_tras_eliminar(tmp_path):
    cliente = ClienteConBloque()
    cliente.delete_object = lambda **kw: cliente.llamadas.append(("delete_object", kw)) or {}
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.codigo_salida == 2 and not informe.limpieza_remota_confirmada


def test_2_si_falla_la_prueba_y_tambien_la_limpieza_remota(tmp_path):
    cliente = ClienteConBloque(versionado=True)
    cliente.error_tras_crear = ReadTimeoutError(endpoint_url="https://x.invalid")
    cliente.errores["delete_object"] = error_cliente("InternalError", 500)
    informe, _ = ejecutar(cliente, tmp_path)
    assert informe.fallos and informe.codigo_salida == 2


def test_2_si_no_se_pudo_borrar_el_archivo_local_aunque_la_prueba_haya_fallado(tmp_path, monkeypatch):
    cliente = ClienteConBloque()
    cliente.errores["put_object"] = error_cliente("AccessDenied", 403)  # la prueba falla
    original = Path.unlink

    def no_borra(self, *a, **k):
        raise PermissionError(f"bloqueado {SECRETO}")

    monkeypatch.setattr(Path, "unlink", no_borra)
    informe, salida = ejecutar(cliente, tmp_path)
    monkeypatch.setattr(Path, "unlink", original)

    assert informe.fallos and informe.codigo_salida == 2 and not informe.limpieza_local_confirmada
    assert any("LIMPIEZA LOCAL PENDIENTE" in p and informe.ruta_local in p for p in informe.pendientes)
    assert SECRETO not in "\n".join(salida)
    Path(informe.ruta_local).unlink()  # limpieza de la propia prueba


def test_1_si_la_generacion_local_falla_no_se_crea_nada_remoto(tmp_path):
    cliente = ClienteConBloque()
    informe, _ = ejecutar(cliente, tmp_path, tamano=1)  # tamaño inválido
    assert informe.codigo_salida == 1 and cliente.llamadas == []
    assert informe.estado_subida is None and informe.limpieza_local_confirmada


# --- main: ejecución explícita, credenciales privadas, cliente cerrado y plazos --------------------------------

def principal(argv, entorno, cliente=None, secretos=None):
    salida: list[str] = []
    pedidos: list[str] = []

    def pedir(mensaje):
        pedidos.append(mensaje)
        return (secretos or {}).get(len(pedidos), "")

    creados = []
    clientes = []

    def crear(config):
        creados.append(config)
        clientes.append(cliente or ClienteConBloque())
        return clientes[-1]

    codigo = script.main(argv, entorno, crear_cliente=crear, pedir_secreto=pedir, imprimir=salida.append)
    return codigo, salida, pedidos, creados, clientes


def test_sin_ejecutar_no_hace_nada_y_no_crea_cliente_ni_pide_credenciales():
    codigo, salida, pedidos, creados, _ = principal([], ENTORNO)
    assert codigo == script.EXIT_FALLO and creados == [] and pedidos == []
    assert any("NO ejecutada" in linea for linea in salida)


def test_faltan_endpoint_o_bucket():
    codigo, salida, _, creados, _ = principal(["--ejecutar"], {"MINIO_ACCESS_KEY": ACCESO, "MINIO_SECRET_KEY": SECRETO})
    assert codigo == script.EXIT_FALLO and creados == []


def test_las_credenciales_se_leen_del_entorno_y_nunca_se_imprimen(tmp_path, monkeypatch):
    monkeypatch.setattr(script.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(script, "TAMANO_OBJETIVO", TAMANO)
    monkeypatch.setattr(script, "ejecutar_prueba", lambda c, b, imprimir: ejecutar_chico(c, b, imprimir))
    codigo, salida, pedidos, creados, _ = principal(["--ejecutar"], ENTORNO)
    assert codigo == script.EXIT_OK and pedidos == []
    [config] = creados
    assert (config.access_key, config.secret_key, config.ambiente) == (ACCESO, SECRETO, "development")
    assert config.endpoint_url == ENTORNO["MINIO_ENDPOINT_URL"] and config.bucket == "bucket-prueba"
    texto = "\n".join(salida)
    assert ACCESO not in texto and SECRETO not in texto and "ejemplo.invalid" not in texto
    assert ACCESO not in repr(config) and SECRETO not in repr(config)


_ejecutar_real = script.ejecutar_prueba


def ejecutar_chico(cliente, bucket, imprimir):
    return _ejecutar_real(cliente, bucket, tamano=TAMANO, imprimir=imprimir)


@pytest.fixture
def prueba_chica(monkeypatch, tmp_path):
    monkeypatch.setattr(script.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(script, "ejecutar_prueba", lambda c, b, imprimir: ejecutar_chico(c, b, imprimir))


def test_sin_credenciales_en_el_entorno_se_piden_sin_eco(prueba_chica):
    entorno = {k: v for k, v in ENTORNO.items() if not k.endswith("_KEY")}
    codigo, salida, pedidos, creados, _ = principal(["--ejecutar"], entorno, secretos={1: ACCESO, 2: SECRETO})
    assert codigo == script.EXIT_OK and len(pedidos) == 2
    assert (creados[0].access_key, creados[0].secret_key) == (ACCESO, SECRETO)
    assert ACCESO not in "\n".join(salida + pedidos) and SECRETO not in "\n".join(salida + pedidos)


def test_no_acepta_credenciales_como_argumentos():
    with pytest.raises(SystemExit):
        principal(["--ejecutar", "--access-key", "x", "--secret-key", "y"], ENTORNO)


def test_endpoint_http_o_con_credenciales_se_rechaza_sin_repetir_el_valor():
    entorno = {**ENTORNO, "MINIO_ENDPOINT_URL": "http://usuario:clave-privada@almacen.invalid"}
    codigo, salida, _, creados, _ = principal(["--ejecutar"], entorno)
    assert codigo == script.EXIT_FALLO and creados == []
    assert "clave-privada" not in "\n".join(salida)


def test_los_argumentos_tienen_prioridad_sobre_el_entorno_y_la_region_es_opcional(prueba_chica):
    _, _, _, creados, _ = principal(
        ["--ejecutar", "--endpoint", "https://otro.ejemplo.invalid", "--bucket", "otro-bucket", "--region", "us-east-1"],
        ENTORNO,
    )
    assert (creados[0].endpoint_url, creados[0].bucket, creados[0].region) == (
        "https://otro.ejemplo.invalid", "otro-bucket", "us-east-1")
    _, _, _, sin_region, _ = principal(["--ejecutar"], ENTORNO)
    assert sin_region[0].region is None


def test_el_cliente_se_cierra_siempre_incluso_si_la_prueba_lanza(prueba_chica, monkeypatch):
    codigo, _, _, _, clientes = principal(["--ejecutar"], ENTORNO)
    assert codigo == script.EXIT_OK and clientes[0].cerrado == 1

    def explota(cliente, bucket, imprimir):
        raise RuntimeError("fallo inesperado")

    monkeypatch.setattr(script, "ejecutar_prueba", explota)
    cliente = ClienteConBloque()
    with pytest.raises(RuntimeError):
        principal(["--ejecutar"], ENTORNO, cliente=cliente)
    assert cliente.cerrado == 1


def test_un_error_al_cerrar_el_cliente_se_informa_sin_cambiar_el_codigo_ni_filtrar_secretos(prueba_chica):
    cliente = ClienteConBloque()
    cliente.close = lambda: (_ for _ in ()).throw(OSError(f"fallo {SECRETO}"))
    codigo, salida, _, _, _ = principal(["--ejecutar"], ENTORNO, cliente=cliente)
    assert codigo == script.EXIT_OK
    texto = "\n".join(salida)
    assert "No se pudo cerrar el cliente S3: OSError" in texto and SECRETO not in texto


def test_el_cliente_sin_close_no_rompe_el_cierre(prueba_chica):
    cliente = ClienteConBloque()
    cliente.close = None
    codigo, *_ = principal(["--ejecutar"], ENTORNO, cliente=cliente)
    assert codigo == script.EXIT_OK


def test_los_plazos_se_describen_con_precision_y_no_se_presenta_un_plazo_total(prueba_chica):
    _, salida, _, creados, _ = principal(["--ejecutar"], ENTORNO)
    config = creados[0]
    assert (config.connect_timeout, config.read_timeout) == (script.CONNECT_TIMEOUT_SEGUNDOS, script.READ_TIMEOUT_SEGUNDOS)
    descripcion = next(linea for linea in salida if linea.startswith("Plazos efectivos"))
    assert "NO hay plazo total" in descripcion and "sin reintentos" in descripcion
    assert "minio_operation_timeout_segundos" not in "\n".join(salida)
    assert "NO existe un plazo total" in script.__doc__ and "NO se aplica aquí" in script.__doc__


def test_el_codigo_de_salida_de_main_es_el_del_informe(prueba_chica):
    cliente = ClienteConBloque(versionado=True)
    cliente.errores["delete_object"] = error_cliente("AccessDenied", 403)
    codigo, salida, *_ = principal(["--ejecutar"], ENTORNO, cliente=cliente)
    assert codigo == script.EXIT_LIMPIEZA_PENDIENTE
    assert any("Código de salida: 2" in linea for linea in salida)


def test_el_script_no_esta_en_la_recoleccion_de_pytest():
    assert not Path(script.__file__).name.startswith("test_")


# --- Ctrl+C a través de main: contrato de salida y resumen siempre visible -----------------------------------

def test_main_ctrl_c_con_limpieza_exitosa_devuelve_1_muestra_el_resumen_y_cierra_el_cliente(prueba_chica):
    cliente = ClienteConBloque(versionado=True)
    cliente.error_tras_crear = KeyboardInterrupt()
    codigo, salida, _, _, _ = principal(["--ejecutar"], ENTORNO, cliente=cliente)
    texto = "\n".join(salida)
    assert codigo == 1 and "=== Resultado ===" in texto and "interrumpida por el usuario" in texto
    assert "Limpieza remota confirmada:                      SÍ" in texto and "Código de salida: 1" in texto
    assert cliente.cerrado == 1 and cliente.entradas == []


def test_main_ctrl_c_con_listado_denegado_devuelve_2_con_clave_version_e_instrucciones(prueba_chica):
    cliente = ClienteConBloque(versionado=True, permitir_listado=False)
    cliente.error_tras_crear = KeyboardInterrupt()
    codigo, salida, _, _, _ = principal(["--ejecutar"], ENTORNO, cliente=cliente)
    texto = "\n".join(salida)
    assert codigo == 2 and "=== Resultado ===" in texto and "Código de salida: 2" in texto
    [objeto] = cliente.entradas  # el objeto sigue ahí: no se borró a ciegas
    assert objeto["Key"] in texto  # la clave
    assert "LIMPIEZA REMOTA PENDIENTE" in texto and "s3:ListBucketVersions" in texto
    assert "Compruebe a mano TODAS las versiones" in texto
    assert borrados(cliente) == [] and cliente.cerrado == 1


def test_main_ctrl_c_con_version_conocida_la_informa_en_las_instrucciones(prueba_chica):
    cliente = ClienteConBloque(versionado=True)

    def interrumpe_al_leer(**kw):  # la subida terminó (versión conocida) y el usuario interrumpe al verificar
        raise KeyboardInterrupt()

    cliente.get_object = interrumpe_al_leer
    cliente.errores["delete_object"] = error_cliente("AccessDenied", 403)
    codigo, salida, _, _, _ = principal(["--ejecutar"], ENTORNO, cliente=cliente)
    texto = "\n".join(salida)
    [objeto] = cliente.entradas
    assert codigo == 2 and f"(versión {objeto['VersionId']})" in texto and "interrumpida" in texto
