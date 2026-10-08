"""Parámetros fijos de la ingesta en código (`parametros.py`): valores acordados, validación, derivación del ambiente desde
`APP_ENV` y garantía de que el entorno ya no los configura. Sin bases de datos; corre también en CI."""
import dataclasses
import math
import re
from datetime import timedelta
from pathlib import Path

import pytest

from app.models.documento_ingesta import VIGENCIA_PREDETERMINADA
from app.services.ingesta.coordinador import ConfigCoordinador
from app.services.ingesta.gestor import OpcionesGestor, configuracion_del_gestor
from app.services.ingesta.parametros import (
    AMBIENTES_VALIDOS,
    PARAMETROS_INGESTA,
    ParametrosIngesta,
    ambiente_desde_app_env,
)

RAIZ = Path(__file__).resolve().parents[1]
RETIRADAS = (
    "INGESTA_TAMANO_LOTE", "INGESTA_TIMEOUT_EMBEDDINGS_SEGUNDOS", "INGESTA_VIGENCIA_MINUTOS", "INGESTA_RECUPERACION_HABILITADA",
    "INGESTA_RECUPERACION_INTERVALO_SEGUNDOS", "INGESTA_CIERRE_ESPERA_SEGUNDOS", "MINIO_PREFIX",
    "MINIO_CONNECT_TIMEOUT_SECONDS", "MINIO_READ_TIMEOUT_SECONDS", "MINIO_OPERATION_TIMEOUT_SECONDS",
    "OCI_CONNECT_TIMEOUT_SECONDS", "OCI_READ_TIMEOUT_SECONDS",
)


def test_los_valores_acordados_estan_en_codigo():
    p = PARAMETROS_INGESTA
    assert p.tamano_lote == 16
    assert p.timeout_embeddings_segundos == 120
    assert p.vigencia == timedelta(minutes=15)
    assert (p.recuperacion_habilitada, p.intervalo_recuperacion_segundos, p.espera_cierre_segundos) == (True, 60, 30)
    assert (p.oci_connect_timeout_segundos, p.oci_read_timeout_segundos) == (10, 60)
    assert (p.minio_connect_timeout_segundos, p.minio_read_timeout_segundos, p.minio_operation_timeout_segundos) == (10, 60, 300)


def test_la_configuracion_es_tipada_inmutable_y_sustituible():
    with pytest.raises(dataclasses.FrozenInstanceError):
        PARAMETROS_INGESTA.tamano_lote = 1
    propios = dataclasses.replace(PARAMETROS_INGESTA, tamano_lote=2, vigencia=timedelta(seconds=30))
    assert propios.tamano_lote == 2 and PARAMETROS_INGESTA.tamano_lote == 16  # sustituir no toca el valor de código


def test_los_parametros_se_traducen_al_coordinador_y_al_gestor_y_coinciden_con_los_valores_por_defecto_de_cada_uno():
    config, opciones = configuracion_del_gestor(PARAMETROS_INGESTA)
    assert config == ConfigCoordinador()  # un solo conjunto de valores, sin divergir
    assert config.vigencia == VIGENCIA_PREDETERMINADA
    assert opciones == OpcionesGestor(recuperacion_habilitada=True, intervalo_recuperacion=60, espera_cierre=30)
    propios = ParametrosIngesta(tamano_lote=3, vigencia=timedelta(minutes=1), recuperacion_habilitada=False, espera_cierre_segundos=0)
    config, opciones = configuracion_del_gestor(propios)
    assert (config.tamano_lote, config.vigencia, opciones.recuperacion_habilitada, opciones.espera_cierre) == (3, timedelta(minutes=1), False, 0)


@pytest.mark.parametrize(
    "cambios",
    [
        {"tamano_lote": 0}, {"tamano_lote": True}, {"tamano_lote": 1.5}, {"tamano_lote": "16"},
        {"vigencia": timedelta(0)}, {"vigencia": 15}, {"vigencia": timedelta(minutes=-1)},
        {"recuperacion_habilitada": "true"}, {"recuperacion_habilitada": 1},
        {"timeout_embeddings_segundos": 0}, {"timeout_embeddings_segundos": -1}, {"timeout_embeddings_segundos": math.inf},
        {"timeout_embeddings_segundos": math.nan}, {"timeout_embeddings_segundos": "120"}, {"timeout_embeddings_segundos": True},
        {"intervalo_recuperacion_segundos": 0}, {"oci_connect_timeout_segundos": 0}, {"oci_read_timeout_segundos": -5},
        {"minio_connect_timeout_segundos": math.inf}, {"minio_read_timeout_segundos": None}, {"minio_operation_timeout_segundos": 0},
        {"espera_cierre_segundos": -1}, {"espera_cierre_segundos": math.nan}, {"espera_cierre_segundos": True},
    ],
)
def test_los_parametros_invalidos_se_rechazan_al_construirlos(cambios):
    with pytest.raises(ValueError):
        ParametrosIngesta(**cambios)


def test_espera_de_cierre_cero_es_valida_y_los_plazos_se_normalizan_a_decimal():
    assert ParametrosIngesta(espera_cierre_segundos=0).espera_cierre_segundos == 0
    ParametrosIngesta(oci_connect_timeout_segundos=1, minio_operation_timeout_segundos=2.5)


@pytest.mark.parametrize("valor", AMBIENTES_VALIDOS)
def test_app_env_valido_es_el_ambiente(valor):
    assert ambiente_desde_app_env(valor) == valor
    assert ambiente_desde_app_env(f"  {valor} ") == valor


@pytest.mark.parametrize("valor", ["prod", "production", "dev", "QA", "Development", "UAT", "", "   ", None, 1, ["qa"]])
def test_app_env_invalido_se_rechaza_sin_repetir_el_valor(valor):
    with pytest.raises(ValueError) as error:
        ambiente_desde_app_env(valor)
    assert str(error.value) == "APP_ENV debe ser exactamente development, qa o uat."


def test_los_ambientes_admitidos_son_solo_los_tres_acordados():
    assert AMBIENTES_VALIDOS == ("development", "qa", "uat")


def test_ningun_modulo_de_la_aplicacion_lee_las_variables_retiradas():
    fuentes = {p: p.read_text(encoding="utf-8") for p in (RAIZ / "app").rglob("*.py")}
    fuentes.update({p: p.read_text(encoding="utf-8") for p in (RAIZ / "scripts").rglob("*.py")})
    for ruta, texto in fuentes.items():
        for nombre in RETIRADAS:
            # Solo se permiten menciones en comentarios/docstrings que expliquen su retiro, nunca una lectura del entorno.
            assert not re.search(rf"""(getenv|environ)[\[\(]\s*["']{nombre}["']""", texto), (ruta.name, nombre)


def test_los_ajustes_de_la_aplicacion_ya_no_declaran_las_variables_retiradas():
    from app.config import settings

    for nombre in RETIRADAS:
        assert not hasattr(settings, nombre), nombre
    assert hasattr(settings, "APP_ENV")


def test_env_example_no_las_incluye_y_conserva_lo_que_si_va_en_el_entorno():
    nombres = {
        linea.split("=", 1)[0].strip()
        for linea in (RAIZ / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in linea and not linea.lstrip().startswith("#")
    }
    assert nombres.isdisjoint(RETIRADAS)
    assert {"APP_ENV", "DATABASE_URL", "VECTOR_DATABASE_URL", "MINIO_ENDPOINT_URL", "MINIO_BUCKET", "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY", "OCI_REGION", "OCI_EMBED_MODEL", "OCI_EMBED_DIMENSIONS", "OCI_COMPARTMENT_ID",
            "OCI_USER_OCID", "OCI_FINGERPRINT", "OCI_TENANCY_OCID", "OCI_KEY_PEM_B64", "OCI_CONFIG_FILE", "OCI_CONFIG_PROFILE"} <= nombres
