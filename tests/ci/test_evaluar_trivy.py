"""Política del stage «Trivy» (ci/trivy/evaluar_trivy.py) con resultados CONTROLADOS de Trivy.

Estas pruebas no ejecutan Trivy ni Docker: demuestran la lógica de decisión (LOW permite; MEDIUM/HIGH/CRITICAL/UNKNOWN bloquean; un error
técnico nunca aprueba). No son evidencia de un escaneo real: el escaneo real se ejecuta con ci/trivy/ejecutar-trivy.sh.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from tests.ci.ayudantes_trivy import (
    ID_CONFIG, ID_INDICE, IMAGEN_REF, TRIVY_IMAGEN, VERSION, reporte, tar_de_imagen, vulnerabilidad,
)

RAIZ = Path(__file__).resolve().parents[2]
_especificacion = importlib.util.spec_from_file_location("evaluar_trivy", RAIZ / "ci" / "trivy" / "evaluar_trivy.py")
evaluar_trivy = importlib.util.module_from_spec(_especificacion)
_especificacion.loader.exec_module(evaluar_trivy)


class Escenario:
    def __init__(self, carpeta):
        self.carpeta = carpeta
        self.tar = carpeta / "imagen.tar"
        self.crudo = carpeta / "trivy-crudo.json"
        self.version = carpeta / "trivy-version.json"
        self.saneado = carpeta / "trivy-report.json"
        self.resumen = carpeta / "resumen-trivy.txt"
        tar_de_imagen(self.tar)
        self.version.write_text(json.dumps(VERSION), encoding="utf-8")

    def escribir(self, datos):
        self.crudo.write_text(json.dumps(datos), encoding="utf-8")
        return self

    def evaluar(self, imagen_id=ID_CONFIG):
        argumentos = [
            "--crudo", str(self.crudo), "--version-trivy", str(self.version), "--tar", str(self.tar), "--imagen-ref", IMAGEN_REF,
            "--imagen-id", imagen_id, "--trivy-imagen", TRIVY_IMAGEN, "--saneado", str(self.saneado), "--resumen", str(self.resumen),
        ]
        return evaluar_trivy.main(argumentos)

    def texto_resumen(self):
        return self.resumen.read_text(encoding="utf-8")


@pytest.fixture
def escenario(tmp_path):
    return Escenario(tmp_path)


# ============================================ LOW permite; MEDIUM, HIGH, CRITICAL y UNKNOWN bloquean ============================================

def test_sin_hallazgos_aprueba(escenario):
    assert escenario.escribir(reporte()).evaluar() == evaluar_trivy.APROBADO
    assert "Veredicto: APROBADO" in escenario.texto_resumen()


def test_solo_low_aprueba_y_queda_en_el_reporte(escenario):
    assert escenario.escribir(reporte([vulnerabilidad("LOW")], [vulnerabilidad("LOW", "CVE-2026-0002", "otro")])).evaluar() == evaluar_trivy.APROBADO
    resumen = escenario.texto_resumen()
    assert "LOW 2" in resumen
    assert len(json.loads(escenario.saneado.read_text(encoding="utf-8"))["Results"]) == 2


@pytest.mark.parametrize("severidad", ["MEDIUM", "HIGH", "CRITICAL"])
@pytest.mark.parametrize("donde", ["os", "app"])
def test_medium_high_y_critical_bloquean_en_sistema_y_en_aplicacion(escenario, severidad, donde):
    hallazgo = [vulnerabilidad(severidad)]
    datos = reporte(os_vulns=hallazgo) if donde == "os" else reporte(lang_vulns=hallazgo)
    assert escenario.escribir(datos).evaluar() == evaluar_trivy.BLOQUEADO
    resumen = escenario.texto_resumen()
    assert "Veredicto: BLOQUEADO" in resumen
    assert f"{severidad} 1" in resumen
    assert "CVE-2026-0001" in resumen


def test_un_hallazgo_sin_version_corregida_tambien_bloquea(escenario):
    assert escenario.escribir(reporte([vulnerabilidad("HIGH")])).evaluar() == evaluar_trivy.BLOQUEADO
    assert "sin versión corregida" in escenario.texto_resumen()


def test_un_hallazgo_con_version_corregida_muestra_la_correccion(escenario):
    assert escenario.escribir(reporte(lang_vulns=[vulnerabilidad("CRITICAL", "CVE-2024-1", "python-jose", "3.4.0")])).evaluar() == evaluar_trivy.BLOQUEADO
    assert "python-jose · 1.0 → 3.4.0" in escenario.texto_resumen()


def test_unknown_no_se_aprueba_automaticamente(escenario):
    assert escenario.escribir(reporte([vulnerabilidad("UNKNOWN")])).evaluar() == evaluar_trivy.BLOQUEADO
    assert "UNKNOWN 1" in escenario.texto_resumen()


@pytest.mark.parametrize("valor", ["", None, "NEGLIGIBLE", "moderate"])
def test_una_severidad_no_reconocida_cuenta_como_unknown_y_bloquea(escenario, valor):
    assert escenario.escribir(reporte([vulnerabilidad(valor)])).evaluar() == evaluar_trivy.BLOQUEADO
    assert "UNKNOWN 1" in escenario.texto_resumen()


def test_low_junto_a_un_medium_bloquea(escenario):
    assert escenario.escribir(reporte([vulnerabilidad("LOW"), vulnerabilidad("MEDIUM", "CVE-2026-0003")])).evaluar() == evaluar_trivy.BLOQUEADO


def test_la_severidad_en_minusculas_tambien_cuenta(escenario):
    assert escenario.escribir(reporte([vulnerabilidad("high")])).evaluar() == evaluar_trivy.BLOQUEADO


def test_el_resumen_trae_conteos_motivo_y_datos_de_trivy(escenario):
    escenario.escribir(reporte([vulnerabilidad("CRITICAL"), vulnerabilidad("LOW", "CVE-2026-0009")], [vulnerabilidad("HIGH", "CVE-2026-0005")])).evaluar()
    resumen = escenario.texto_resumen()
    for esperado in (
        IMAGEN_REF, ID_CONFIG, "Trivy: 0.76.1", TRIVY_IMAGEN, "versión 2", "2026-10-28T01:21:08Z", "debian 13.7",
        "CRITICAL 1 · HIGH 1 · MEDIUM 0 · LOW 1 · UNKNOWN 0", "Motivo: hallazgos que bloquean el despliegue: 1 CRITICAL, 1 HIGH",
        "1 paquetes del sistema operativo y 1 dependencias de aplicación",
    ):
        assert esperado in resumen


# ============================================ el error técnico NUNCA aprueba ============================================

def comprobar_error_tecnico(escenario, codigo, motivo):
    assert codigo == evaluar_trivy.ERROR_TECNICO
    resumen = escenario.texto_resumen()
    assert "ERROR TÉCNICO" in resumen
    assert motivo in resumen
    assert "APROBADO" not in resumen
    assert not escenario.saneado.exists(), "un error técnico no deja un reporte que aparente un análisis válido"


def test_sin_reporte_es_error_tecnico(escenario):
    comprobar_error_tecnico(escenario, escenario.evaluar(), "reporte de Trivy ausente o ilegible")


def test_reporte_con_json_invalido_es_error_tecnico(escenario):
    escenario.crudo.write_text("{no es json", encoding="utf-8")
    comprobar_error_tecnico(escenario, escenario.evaluar(), "ausente o ilegible")


def test_reporte_que_no_es_un_objeto_es_error_tecnico(escenario):
    escenario.crudo.write_text("[]", encoding="utf-8")
    comprobar_error_tecnico(escenario, escenario.evaluar(), "no tiene la forma esperada")


def test_reporte_de_otra_imagen_es_error_tecnico(escenario):
    datos = reporte()
    datos["Metadata"]["ImageID"] = "sha256:" + "d" * 64
    comprobar_error_tecnico(escenario, escenario.escribir(datos).evaluar(), "ImageID distinto")


def test_tar_que_no_es_la_imagen_registrada_es_error_tecnico(escenario):
    comprobar_error_tecnico(escenario, escenario.escribir(reporte()).evaluar(imagen_id="sha256:" + "e" * 64), "no corresponde al ID")


def test_id_de_imagen_invalido_es_error_tecnico(escenario):
    comprobar_error_tecnico(escenario, escenario.escribir(reporte()).evaluar(imagen_id="latest"), "no es un digest sha256")


def test_sin_paquetes_del_sistema_operativo_es_error_tecnico(escenario):
    datos = reporte()
    datos["Results"] = [r for r in datos["Results"] if r["Class"] != "os-pkgs"]
    comprobar_error_tecnico(escenario, escenario.escribir(datos).evaluar(), "paquetes del sistema operativo")


def test_sin_dependencias_de_aplicacion_es_error_tecnico(escenario):
    datos = reporte()
    datos["Results"] = [r for r in datos["Results"] if r["Class"] != "lang-pkgs"]
    comprobar_error_tecnico(escenario, escenario.escribir(datos).evaluar(), "dependencias de aplicación")


def test_sin_sistema_operativo_identificado_es_error_tecnico(escenario):
    datos = reporte()
    datos["Metadata"]["OS"] = {}
    comprobar_error_tecnico(escenario, escenario.escribir(datos).evaluar(), "sistema operativo de la imagen")


def test_hallazgos_suprimidos_son_error_tecnico(escenario):
    datos = reporte([vulnerabilidad("HIGH")], ExperimentalModifiedFindings=[{"Type": "vulnerability", "Status": "not_affected"}])
    comprobar_error_tecnico(escenario, escenario.escribir(datos).evaluar(), "no admite supresiones")


@pytest.mark.parametrize("cambio", [{"SchemaVersion": 1}, {"ArtifactType": "filesystem"}, {"Results": None}])
def test_reportes_con_forma_inesperada_son_error_tecnico(escenario, cambio):
    assert escenario.escribir(reporte(**cambio)).evaluar() == evaluar_trivy.ERROR_TECNICO
    assert "APROBADO" not in escenario.texto_resumen()


def test_sin_informacion_de_la_base_de_vulnerabilidades_es_error_tecnico(escenario):
    escenario.version.write_text(json.dumps({"Version": "0.76.1"}), encoding="utf-8")
    comprobar_error_tecnico(escenario, escenario.escribir(reporte()).evaluar(), "base de vulnerabilidades")


def test_sin_archivo_de_version_es_error_tecnico(escenario):
    escenario.version.unlink()
    comprobar_error_tecnico(escenario, escenario.escribir(reporte()).evaluar(), "versión de Trivy ausente")


def test_tar_ilegible_es_error_tecnico(escenario):
    escenario.tar.write_bytes(b"no es un tar")
    comprobar_error_tecnico(escenario, escenario.escribir(reporte()).evaluar(), "no se pudo leer el manifiesto")


def test_tar_con_mas_de_una_imagen_es_error_tecnico(escenario):
    tar_de_imagen(escenario.tar, imagenes=2)
    comprobar_error_tecnico(escenario, escenario.escribir(reporte()).evaluar(), "exactamente una imagen")


# ============================================ identidad de la imagen y saneado ============================================

def test_el_id_del_indice_oci_identifica_la_imagen_en_el_almacen_containerd(escenario):
    tar_de_imagen(escenario.tar, con_indice=True)
    assert escenario.escribir(reporte()).evaluar(imagen_id=ID_INDICE) == evaluar_trivy.APROBADO


def test_el_id_de_configuracion_identifica_la_imagen_en_el_almacen_clasico(escenario):
    assert escenario.escribir(reporte()).evaluar(imagen_id=ID_CONFIG) == evaluar_trivy.APROBADO


def test_el_json_archivado_no_lleva_la_configuracion_de_la_imagen_y_nombra_la_imagen_real(escenario):
    original = reporte([vulnerabilidad("LOW")])
    escenario.escribir(original).evaluar()
    archivado = json.loads(escenario.saneado.read_text(encoding="utf-8"))
    assert "ImageConfig" not in archivado["Metadata"]
    assert "SECRETO" not in escenario.saneado.read_text(encoding="utf-8")
    assert archivado["ArtifactName"] == IMAGEN_REF
    assert archivado["Results"] == original["Results"]  # los hallazgos no se alteran
    assert "ImageConfig" in json.loads(escenario.crudo.read_text(encoding="utf-8"))["Metadata"]  # el crudo de entrada no se modifica
