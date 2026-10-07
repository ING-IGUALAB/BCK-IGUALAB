"""Catálogo GRI local y versionado (decisión 2026-10-07).

Nivel: unidad, sin red ni BD. Comprueba el esquema, la separación entre versión del
catálogo y edición de cada estándar, y que no se confunden los GRI 101/102/103
históricos con los Topic Standards nuevos. NO comprueba el contenido contra
globalreporting.org: esa verificación fue manual y consta en cada entrada.
"""
import copy
import json
from pathlib import Path

import pytest

from app.services.ingesta import catalogo_gri
from app.services.ingesta.catalogo_gri import (
    CatalogoGriInvalido,
    cargar_catalogo,
    catalogo_predeterminado,
    interpretar_catalogo,
)


@pytest.fixture(scope="module")
def datos():
    return json.loads(catalogo_gri.RUTA_CATALOGO_PREDETERMINADA.read_text(encoding="utf-8"))


def test_el_catalogo_empaquetado_carga_y_su_cobertura_coincide_con_las_entradas():
    catalogo = catalogo_predeterminado()
    cobertura = catalogo.cobertura
    assert cobertura["total_entradas"] == len(catalogo.entradas)
    por_vigencia = {}
    for entrada in catalogo.entradas:
        por_vigencia[entrada.vigencia] = por_vigencia.get(entrada.vigencia, 0) + 1
    assert cobertura["por_vigencia"] == por_vigencia
    # Declara lo que NO cubre y no pretende ser «todos los GRI».
    assert cobertura["no_cubierto"]
    assert "NO" in cobertura["declaracion"] and "todos los GRI" in cobertura["declaracion"]


def test_version_del_catalogo_es_independiente_de_la_edicion_de_cada_estandar():
    catalogo = catalogo_predeterminado()
    assert catalogo.version_catalogo == "2026-10-07.2"
    ediciones = {e.edicion for e in catalogo.entradas}
    assert catalogo.version_catalogo not in ediciones
    assert {"2016", "2018", "2019", "2020", "2021", "2022", "2024", "2025"} <= ediciones


def test_todas_las_entradas_tienen_los_campos_pedidos():
    for entrada in catalogo_predeterminado().entradas:
        assert entrada.codigo and entrada.nombre and entrada.edicion and entrada.categoria
        assert entrada.metodo_verificacion and entrada.fuente and entrada.verificado_en
        if entrada.url_oficial is not None:
            assert entrada.url_oficial.startswith(("https://www.globalreporting.org/", "https://globalreporting.org/"))
        if entrada.url_comprobada:
            assert entrada.url_oficial is not None


def test_gri_305_y_los_sectoriales_de_interes():
    catalogo = catalogo_predeterminado()
    emisiones = catalogo.buscar("305", "2016")
    assert (emisiones.nombre, emisiones.categoria, emisiones.vigencia) == ("Emissions", "Ambiental", "vigente")
    mineria = catalogo.buscar("14", "2024")
    assert (mineria.nombre, mineria.tipo, mineria.version_documento) == ("Mining Sector", "sectorial", "1.1")
    assert catalogo.buscar("11", "2021").nombre == "Oil and Gas Sector"


def test_el_antiguo_gri_102_no_es_el_estandar_de_cambio_climatico():
    catalogo = catalogo_predeterminado()
    antiguo = catalogo.buscar("102", "2016")
    nuevo = catalogo.buscar("102", "2025")
    assert (antiguo.nombre, antiguo.tipo, antiguo.vigencia) == ("General Disclosures", "universal", "historico")
    assert (nuevo.nombre, nuevo.tipo, nuevo.vigencia) == ("Climate Change", "tematico", "publicado_no_vigente")
    assert nuevo.efectiva_desde == "2027-01-01"
    # Lo mismo para 101 (Foundation / Biodiversity) y 103 (Management Approach / Energy).
    assert catalogo.buscar("101", "2016").nombre == "Foundation" and catalogo.buscar("101", "2024").nombre == "Biodiversity"
    assert catalogo.buscar("103", "2016").nombre == "Management Approach" and catalogo.buscar("103", "2025").nombre == "Energy"
    assert {e.edicion for e in catalogo.por_codigo("102")} == {"2016", "2025"}


def test_ediciones_distintas_del_mismo_codigo_no_se_funden():
    catalogo = catalogo_predeterminado()
    assert catalogo.buscar("306", "2016").nombre == "Effluents and Waste"
    assert catalogo.buscar("306", "2020").nombre == "Waste"
    assert catalogo.buscar("306", "2018") is None


def test_estandares_retirados_se_marcan_sin_convertirlos_en_otro_codigo():
    catalogo = catalogo_predeterminado()
    for codigo in ("307", "412", "419"):
        (entrada,) = catalogo.por_codigo(codigo)
        assert entrada.vigencia == "retirado"
        assert entrada.reemplazado_por == ()  # el catálogo no decide equivalencias de revelaciones
    assert catalogo.buscar("304", "2016").reemplazado_por == (("101", "2024"),)


def test_el_catalogo_no_se_presenta_como_el_de_40_codigos(datos):
    # Hay 40 estándares vigentes por coincidencia: no es el seed corporativo pendiente.
    assert datos["cobertura"]["declaracion"].startswith("Este catálogo NO es el catálogo corporativo de 40 códigos")


def test_el_modulo_no_usa_red():
    fuente = Path(catalogo_gri.__file__).read_text(encoding="utf-8")
    for prohibido in ("import requests", "import httpx", "urllib", "socket", "aiohttp"):
        assert prohibido not in fuente


# --- Validación del esquema ----------------------------------------------------------

def con(datos, **cambios):
    copia = copy.deepcopy(datos)
    for clave, valor in cambios.items():
        copia[clave] = valor
    return copia


def test_esquema_no_soportado(datos):
    with pytest.raises(CatalogoGriInvalido, match="Esquema"):
        interpretar_catalogo(con(datos, esquema=2))


def test_entrada_duplicada_se_rechaza(datos):
    entradas = copy.deepcopy(datos["entradas"])
    entradas.append(copy.deepcopy(entradas[0]))
    cobertura = {**datos["cobertura"], "total_entradas": len(entradas)}
    with pytest.raises(CatalogoGriInvalido, match="duplicadas"):
        interpretar_catalogo(con(datos, entradas=entradas, cobertura=cobertura))


def test_cobertura_inconsistente_se_rechaza(datos):
    with pytest.raises(CatalogoGriInvalido, match="cobertura"):
        interpretar_catalogo(con(datos, cobertura={**datos["cobertura"], "total_entradas": 999}))


@pytest.mark.parametrize(
    ("cambio", "mensaje"),
    [
        ({"codigo": "0305"}, "código"),
        ({"codigo": "3050"}, "código"),
        ({"edicion": "16"}, "edición"),
        ({"vigencia": "otra"}, "vigencia"),
        ({"tipo": "otro"}, "tipo"),
        ({"url_oficial": "https://ejemplo.com/gri.pdf"}, "globalreporting.org"),
        ({"efectiva_desde": "01/01/2026"}, "AAAA-MM-DD"),
        ({"reemplazado_por": [{"codigo": "999", "edicion": "2099"}]}, "inexistente"),
        ({"nombre": ""}, "nombre"),
    ],
)
def test_entradas_mal_formadas_se_rechazan(datos, cambio, mensaje):
    entradas = copy.deepcopy(datos["entradas"])
    entradas[10].update(cambio)
    with pytest.raises(CatalogoGriInvalido, match=mensaje):
        interpretar_catalogo(con(datos, entradas=entradas))


def test_url_comprobada_exige_url(datos):
    entradas = copy.deepcopy(datos["entradas"])
    entradas[10]["url_oficial"] = None
    entradas[10]["verificacion"]["url_comprobada"] = True
    with pytest.raises(CatalogoGriInvalido, match="URL"):
        interpretar_catalogo(con(datos, entradas=entradas))


def test_archivo_ilegible_no_filtra_la_ruta(tmp_path):
    with pytest.raises(CatalogoGriInvalido) as capturado:
        cargar_catalogo(tmp_path / "no-existe.json")
    assert str(tmp_path) not in str(capturado.value)
    ruta = tmp_path / "roto.json"
    ruta.write_text("{no es json", encoding="utf-8")
    with pytest.raises(CatalogoGriInvalido):
        cargar_catalogo(ruta)


# --- Publicado no es vigente ---------------------------------------------------------------------

def test_publicado_no_es_lo_mismo_que_vigente():
    catalogo = catalogo_predeterminado()
    corte = catalogo.recopilado_en
    for codigo in ("102", "103"):
        entrada = catalogo.buscar(codigo, "2025")
        assert entrada.vigencia == "publicado_no_vigente" and entrada.efectiva_desde == "2027-01-01"
        assert entrada.vigente_en(corte) is False and entrada.vigente_en("2027-01-01") is True
    # GRI 101 (2024) ya entró en vigor el 2026-01-01; GRI 14 (2024 V1.1) también.
    assert catalogo.buscar("101", "2024").vigente_en(corte) is True
    assert catalogo.buscar("14", "2024").efectiva_desde == "2026-01-01"
    cobertura = catalogo.cobertura
    assert cobertura["publicados_no_vigentes_aun"] == 2
    assert cobertura["publicados_por_gri"] == len(catalogo.entradas) - cobertura["por_vigencia"]["retirado"]
    assert cobertura["en_vigor_en_la_fecha_de_corte"] == cobertura["por_vigencia"]["vigente"] + cobertura["por_vigencia"]["parcialmente_vigente"]
    assert cobertura["en_vigor_en_la_fecha_de_corte"] < cobertura["publicados_por_gri"]
    assert "Publicado no es vigente" in cobertura["distincion"]


def test_fechas_efectivas_leidas_de_los_pdf_oficiales():
    catalogo = catalogo_predeterminado()
    esperadas = {
        ("1", "2021"): "2023-01-01", ("201", "2016"): "2018-07-01", ("207", "2019"): "2021-01-01",
        ("303", "2018"): "2021-01-01", ("306", "2020"): "2022-01-01", ("403", "2018"): "2021-01-01",
        ("11", "2021"): "2023-01-01", ("12", "2022"): "2024-01-01", ("13", "2022"): "2024-01-01",
    }
    for (codigo, edicion), fecha in esperadas.items():
        assert catalogo.buscar(codigo, edicion).efectiva_desde == fecha
    # Los estándares con PDF comprobado tienen fecha efectiva; el método lo indica.
    for entrada in catalogo.entradas:
        if entrada.url_comprobada:
            assert entrada.efectiva_desde is not None and entrada.metodo_verificacion == "pdf_oficial_texto"


def test_estandares_parcial_o_proximamente_reemplazados():
    catalogo = catalogo_predeterminado()
    energia = catalogo.buscar("302", "2016")
    assert (energia.vigencia, energia.efectiva_hasta, energia.reemplazado_por) == ("vigente", "2026-12-31", (("103", "2025"),))
    assert energia.vigente_en("2026-12-31") is True and energia.vigente_en("2027-01-01") is False
    emisiones = catalogo.buscar("305", "2016")
    assert emisiones.vigencia == "vigente" and emisiones.reemplazo_parcial and emisiones.reemplazo_efectivo_desde == "2027-01-01"
    residuos = catalogo.buscar("306", "2016")
    assert residuos.vigencia == "parcialmente_vigente" and residuos.reemplazo_parcial
    assert residuos.vigente_en(catalogo.recopilado_en) is True  # 306-3 sigue en vigor
    assert catalogo.buscar("304", "2016").vigente_en("2026-01-01") is None  # sin fecha efectiva verificada


def test_vigente_en_exige_una_fecha_valida_y_los_retirados_nunca_estan_en_vigor():
    catalogo = catalogo_predeterminado()
    assert catalogo.buscar("307", "2016").vigente_en("2020-01-01") is False
    with pytest.raises(ValueError):
        catalogo.buscar("305", "2016").vigente_en("2026/10/07")


@pytest.mark.parametrize(
    ("codigo", "edicion", "vigencia"),
    [("102", "2025", "vigente"), ("101", "2024", "publicado_no_vigente"), ("304", "2016", "vigente"),
     ("14", "2024", "historico"), ("302", "2016", "historico")],
)
def test_una_vigencia_incoherente_con_las_fechas_se_rechaza(datos, codigo, edicion, vigencia):
    entradas = copy.deepcopy(datos["entradas"])
    entrada = next(e for e in entradas if (e["codigo"], e["edicion"]) == (codigo, edicion))
    entrada["vigencia"] = vigencia
    cobertura = copy.deepcopy(datos["cobertura"])
    por_vigencia = {}
    for e in entradas:
        por_vigencia[e["vigencia"]] = por_vigencia.get(e["vigencia"], 0) + 1
    cobertura["por_vigencia"] = por_vigencia
    with pytest.raises(CatalogoGriInvalido, match="vigencia"):
        interpretar_catalogo(con(datos, entradas=entradas, cobertura=cobertura))


def test_la_cobertura_declarada_debe_distinguir_publicados_y_en_vigor(datos):
    cobertura = {**datos["cobertura"], "en_vigor_en_la_fecha_de_corte": 45}
    with pytest.raises(CatalogoGriInvalido, match="en_vigor"):
        interpretar_catalogo(con(datos, cobertura=cobertura))
