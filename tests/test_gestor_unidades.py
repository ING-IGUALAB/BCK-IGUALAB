"""Piezas del gestor de ingesta que no necesitan bases de datos: copia acotada del archivo, validación de opciones,
mensajes seguros de configuración y el esquema OpenAPI del contrato. Se ejecutan también en CI (sin PostgreSQL)."""
import pytest

from app.exceptions import ExternalServiceError
from app.services.ingesta import gestor as modulo
from app.services.ingesta.gestor import (
    FuenteEnMemoria,
    OpcionesGestor,
    copiar_archivo_acotado,
)
from app.services.ingesta.reglas import TAMANO_MAXIMO_BYTES


def lector_de(datos: bytes, bloque_maximo: int | None = None):
    posicion = 0
    llamadas = []

    async def leer(cantidad: int) -> bytes:
        nonlocal posicion
        cantidad = min(cantidad, bloque_maximo or cantidad)
        llamadas.append(cantidad)
        trozo = datos[posicion:posicion + cantidad]
        posicion += len(trozo)
        return trozo

    leer.llamadas = llamadas
    return leer


async def drenar(fuente: FuenteEnMemoria, bloque: int = 7) -> bytes:
    salida = bytearray()
    while trozo := await fuente.leer(bloque):
        salida += trozo
    return bytes(salida)


async def test_la_copia_acotada_conserva_los_bytes_y_suelta_la_memoria_al_terminar():
    datos = b"# T\n" + "cuerpo con acentos: áéíóú ñ\n".encode("utf-8") * 20
    fuente = await copiar_archivo_acotado(lector_de(datos, bloque_maximo=50))
    assert fuente.tamano == len(datos)
    assert await drenar(fuente) == datos
    assert fuente.tamano == 0 and await fuente.leer(10) == b""  # tras leer hasta el final, soltó el archivo


async def test_la_copia_acotada_no_lee_un_archivo_ilimitado_pero_deja_pasar_el_exceso_al_validador():
    bloque = modulo.TAMANO_BLOQUE_COPIA

    async def infinito(cantidad: int) -> bytes:
        return b"a" * cantidad

    fuente = await copiar_archivo_acotado(infinito)
    # Se detiene en cuanto supera el límite (a lo sumo un bloque de más): el validador cuenta y responde 413.
    assert TAMANO_MAXIMO_BYTES < fuente.tamano <= TAMANO_MAXIMO_BYTES + bloque


async def test_un_archivo_exactamente_en_el_limite_se_copia_completo():
    fuente = await copiar_archivo_acotado(lector_de(b"b" * TAMANO_MAXIMO_BYTES))
    assert fuente.tamano == TAMANO_MAXIMO_BYTES


async def test_un_archivo_vacio_se_copia_como_vacio():
    fuente = await copiar_archivo_acotado(lector_de(b""))
    assert fuente.tamano == 0 and await fuente.leer(5) == b""


@pytest.mark.parametrize(
    "cambios",
    [{"intervalo_recuperacion": 0}, {"intervalo_recuperacion": -1}, {"espera_cierre": -1}, {"retraso_primer_barrido": -0.1},
     {"espera_cierre": True}, {"intervalo_recuperacion": "60"}, {"espera_cierre": None}],
)
def test_opciones_invalidas_se_rechazan(cambios):
    with pytest.raises(ValueError):
        OpcionesGestor(**cambios)


def test_opciones_validas_incluyen_cero_para_esperar_y_retrasar():
    opciones = OpcionesGestor(espera_cierre=0, retraso_primer_barrido=0)
    assert opciones.espera_cierre == 0 and opciones.recuperacion_habilitada is True


@pytest.mark.parametrize(
    "exc,esperado",
    [
        (ValueError("Falta la configuración de almacenamiento: MINIO_BUCKET."), "Falta la configuración de almacenamiento: MINIO_BUCKET."),
        (ValueError("El endpoint debe ser una URL https sin credenciales, consulta ni fragmento."), None),
        (ValueError("https://usuario:clave@host/ruta"), "ValueError"),  # una URL (con posible credencial) nunca se repite
        (ValueError('contiene "comillas" y /barras/'), "ValueError"),
        (RuntimeError("Falta algo: SECRETO"), "RuntimeError"),  # solo ValueError se considera mensaje propio
        (ExternalServiceError("VECTOR_DATABASE_NOT_CONFIGURED", "La base de datos vectorial no está configurada."), "VECTOR_DATABASE_NOT_CONFIGURED"),
    ],
)
def test_el_motivo_de_configuracion_nunca_repite_valores(exc, esperado):
    motivo = modulo._motivo_seguro(exc)
    if esperado is not None:
        assert motivo == esperado
    assert "SECRETO" not in motivo and "clave@" not in motivo and "://" not in motivo


def test_el_openapi_publica_el_contrato_de_ingesta():
    from app.main import app

    esquema = app.openapi()
    rutas = esquema["paths"]
    assert set(rutas["/documentos/operaciones"]) == {"post"}
    assert set(rutas["/documentos/operaciones/{operacion_id}"]) == {"get"}
    assert set(rutas["/documentos/operaciones/{operacion_id}/ingesta"]) == {"post"}
    assert set(rutas["/documentos/operaciones/{operacion_id}/reintentar-publicacion"]) == {"post"}
    assert set(rutas["/documentos"]) == {"get"} and set(rutas["/documentos/{documento_id}"]) == {"get"}

    cuerpo = rutas["/documentos/operaciones/{operacion_id}/ingesta"]["post"]["requestBody"]["content"]["multipart/form-data"]
    campos = esquema["components"]["schemas"][cuerpo["schema"]["$ref"].rsplit("/", 1)[1]]
    assert set(campos["required"]) == {"archivo", "empresa_id", "anio", "tipo_documento"}
    assert campos["properties"]["archivo"]["format"] == "binary"
    assert {"201", "401", "403", "409", "413", "422", "500", "502", "503", "504"} <= set(
        rutas["/documentos/operaciones/{operacion_id}/ingesta"]["post"]["responses"]
    )
    estados = esquema["components"]["schemas"]["EstadoOperacionPublico"]["enum"]
    assert "PUBLICACION_PENDIENTE" in estados and "COMPLETADO" in estados
