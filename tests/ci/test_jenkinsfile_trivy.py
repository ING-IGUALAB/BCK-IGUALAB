"""Garantías ESTRUCTURALES del control Trivy en el Jenkinsfile y en los scripts de ci/ (análisis estático del texto).

No ejecutan Jenkins: comprueban orden, ramas, ausencia de mecanismos de omisión y que ningún despliegue reconstruye la imagen. Que Jenkins
funcione de verdad solo se demuestra con una ejecución del pipeline.
"""
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
JENKINSFILE = (RAIZ / "Jenkinsfile").read_text(encoding="utf-8")
SCRIPTS_SH = sorted((RAIZ / "ci").rglob("*.sh"))
RAMAS_CON_DESPLIEGUE = {"development", "qa", "uat"}
SCRIPT_TRIVY = RAIZ / "ci" / "trivy" / "ejecutar-trivy.sh"
STAGE_INTEGRACION = "Integration Tests"
STAGES_DE_DESPLIEGUE = ("Deploy Dev (Docker Compose)", "Deploy QA (Docker Compose)", "Deploy UAT (Docker Compose)")


def nombres_de_stages():
    return re.findall(r"^\s*stage\('([^']+)'\)", JENKINSFILE, flags=re.MULTILINE)


def bloque_del_stage(nombre):
    """Texto del stage `nombre` hasta el siguiente stage de primer nivel."""
    inicio = JENKINSFILE.index(f"stage('{nombre}')")
    siguiente = re.search(r"^        stage\('", JENKINSFILE[inicio + 1:], flags=re.MULTILINE)
    fin = inicio + 1 + siguiente.start() if siguiente else len(JENKINSFILE)
    return JENKINSFILE[inicio:fin]


def ramas_del_when(nombre):
    cuando = re.search(r"when \{(.*?)\n            \}", bloque_del_stage(nombre), flags=re.DOTALL)
    return set(re.findall(r"branch '([^']+)'", cuando.group(1)))


def sin_comentarios(texto):
    return "\n".join(linea for linea in texto.splitlines() if not linea.lstrip().startswith("//"))


def lineas_ejecutables(ruta):
    return [linea for linea in ruta.read_text(encoding="utf-8").splitlines() if linea.strip() and not linea.lstrip().startswith("#")]


# ============================================ posición y condiciones del stage ============================================

def test_trivy_va_despues_de_construir_y_antes_de_cualquier_despliegue():
    stages = nombres_de_stages()
    assert stages.index("Quality Gate") < stages.index("Construir imagen") < stages.index("Trivy")
    for despliegue in STAGES_DE_DESPLIEGUE:
        assert stages.index("Trivy") < stages.index(despliegue), despliegue


def test_trivy_aplica_a_todas_las_ramas_con_despliegue():
    for despliegue, rama in zip(STAGES_DE_DESPLIEGUE, ("development", "qa", "uat")):
        assert ramas_del_when(despliegue) == {rama}
    assert ramas_del_when("Trivy") == RAMAS_CON_DESPLIEGUE
    assert ramas_del_when("Construir imagen") == RAMAS_CON_DESPLIEGUE


def test_se_conservan_sonar_integracion_e2e_y_zap_con_su_orden_y_condiciones():
    stages = nombres_de_stages()
    for conservado in ("SonarQube Analysis", "Quality Gate", STAGE_INTEGRACION, "E2E Tests", "OWASP ZAP"):
        assert conservado in stages
    assert stages.index(STAGE_INTEGRACION) < stages.index("E2E Tests") < stages.index("OWASP ZAP")
    assert ramas_del_when("SonarQube Analysis") == {"qa", "uat"}
    for solo_qa in (STAGE_INTEGRACION, "E2E Tests", "OWASP ZAP"):
        assert ramas_del_when(solo_qa) == {"qa"}


# ============================================ el servicio actual no se toca antes de aprobar ============================================

OPERACIONES_QUE_CAMBIAN_EL_SERVICIO = r"\b(down|up|build|stop|start|restart|rm|kill|pause|create|run|pull|push)\b"


def test_antes_de_trivy_no_hay_ninguna_operacion_de_compose_que_cambie_el_servicio():
    """Antes del despliegue solo se admiten consultas de solo lectura (`ps`, `logs`, `exec`...); nada que detenga, reemplace o reconstruya el servicio."""
    previo = sin_comentarios(JENKINSFILE[: JENKINSFILE.index("stage('Deploy Dev (Docker Compose)')")])
    llamadas = [linea for linea in previo.splitlines() if "docker compose" in linea or "docker-compose" in linea]
    for linea in llamadas:
        assert not re.search(OPERACIONES_QUE_CAMBIAN_EL_SERVICIO, linea.split("compose", 1)[1]), linea.strip()


def test_el_jenkinsfile_no_construye_ni_detiene_el_servicio_por_su_cuenta():
    codigo = sin_comentarios(JENKINSFILE)
    assert not re.search(r"compose[^\n]*\bdown\b", codigo)
    assert not re.search(r"compose[^\n]*up[^\n]*--build", codigo)
    assert "--build" not in codigo


def test_los_tres_despliegues_usan_el_script_de_la_imagen_analizada():
    for despliegue in STAGES_DE_DESPLIEGUE:
        assert "ci/desplegar-imagen-analizada.sh" in bloque_del_stage(despliegue), despliegue


def test_el_unico_down_esta_en_el_script_de_despliegue_y_la_imagen_no_se_reconstruye():
    ejecutable = "\n".join(lineas_ejecutables(RAIZ / "ci" / "desplegar-imagen-analizada.sh"))
    assert "compose -p \"$COMPOSE_PROJECT\" down" in ejecutable
    assert "up -d --no-build --pull never" in ejecutable
    assert "--build" not in ejecutable.replace("--no-build", "")
    for otro in SCRIPTS_SH:
        if otro.name != "desplegar-imagen-analizada.sh":
            assert not re.search(r"compose[^\n]*\bdown\b", "\n".join(lineas_ejecutables(otro))), otro.name


def test_el_compose_toma_la_imagen_de_backend_image():
    compose = (RAIZ / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s+image: \$\{BACKEND_IMAGE:-[^}]+\}", compose, flags=re.MULTILINE)


# ============================================ sin bypass ============================================

def test_el_stage_trivy_no_tiene_omisiones_ni_estado_inestable():
    bloque = bloque_del_stage("Trivy")
    codigo = "\n".join(linea for linea in bloque.splitlines() if not linea.lstrip().startswith("//"))
    for prohibido in ("unstable", "catchError", "|| true", "ignoreFailure", "allowFailure", "buildResult"):
        assert prohibido not in codigo, prohibido
    assert "error(" in codigo
    assert "codigo == 1" in codigo and "codigo != 0" in codigo


@pytest.mark.parametrize("ruta", SCRIPTS_SH, ids=lambda r: r.name)
def test_los_scripts_de_ci_no_contienen_omisiones_de_la_politica(ruta):
    ejecutable = "\n".join(lineas_ejecutables(ruta))
    for prohibido in (
        "--ignore-unfixed", "--ignore-status", "--vex", "--skip-db-update", "--skip-dirs", "--skip-files", "--skip-java-db-update",
        "|| true", "--offline-scan", "-e TRIVY_", "export TRIVY_", "--exit-on-eol",
    ):
        assert prohibido not in ejecutable, f"{ruta.name}: {prohibido}"


def test_el_escaneo_incluye_todas_las_severidades_en_un_solo_json_y_no_sube_el_nivel_de_aprobacion():
    ejecutable = "\n".join(lineas_ejecutables(SCRIPT_TRIVY))
    assert "--severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL" in ejecutable
    assert "--scanners vuln" in ejecutable and "--pkg-types os,library" in ejecutable
    assert "--format json" in ejecutable and "--exit-code 0" in ejecutable
    assert ejecutable.count("image --input") == 1, "un único análisis: el HTML sale de convert, no de otro escaneo"
    assert "convert --format template --template \"@/contrib/html.tpl\"" in ejecutable
    assert '--ignorefile ""' in ejecutable


def test_las_imagenes_de_trivy_y_del_evaluador_estan_fijadas_por_version_y_digest():
    texto = (SCRIPT_TRIVY).read_text(encoding="utf-8")
    assert re.search(r'^TRIVY_IMAGEN="ghcr\.io/aquasecurity/trivy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}"$', texto, flags=re.MULTILINE)
    assert re.search(r'^PYTHON_IMAGEN="python:[\w.-]+@sha256:[0-9a-f]{64}"$', texto, flags=re.MULTILINE)
    assert ":latest" not in texto


def test_el_contenedor_de_trivy_no_monta_el_socket_de_docker():
    assert "docker.sock" not in "\n".join(lineas_ejecutables(SCRIPT_TRIVY))


# ============================================ reportes y retención ============================================

def test_se_conserva_la_retencion_de_un_build_y_un_artifact():
    assert "buildDiscarder(logRotator(numToKeepStr: '1', artifactNumToKeepStr: '1'))" in JENKINSFILE


def test_trivy_archiva_siempre_y_antes_de_la_limpieza_del_workspace():
    bloque = bloque_del_stage("Trivy")
    post = bloque[bloque.index("post {"):]
    assert "always" in post and "archiveArtifacts artifacts: 'trivy-resultados/**'" in post
    assert "allowEmptyArchive: true" in post
    assert JENKINSFILE.index("archiveArtifacts artifacts: 'trivy-resultados/**'") < JENKINSFILE.index("deleteDir()")


def test_solo_se_archiva_trivy_resultados_no_el_tar_ni_el_crudo_ni_el_env():
    archivados = re.findall(r"archiveArtifacts artifacts: '([^']+)'", bloque_del_stage("Trivy"))
    assert archivados == ["trivy-resultados/**"]
    ejecutable = "\n".join(lineas_ejecutables(SCRIPT_TRIVY))
    assert "trap 'rm -rf \"$TRABAJO\"' EXIT" in ejecutable


@pytest.mark.parametrize("archivo", [".gitignore", ".dockerignore"])
def test_los_directorios_de_trabajo_de_trivy_estan_ignorados(archivo):
    lineas = (RAIZ / archivo).read_text(encoding="utf-8").splitlines()
    for directorio in ("trivy-resultados/", "trivy-trabajo/", "ci-estado/"):
        assert directorio in lineas, f"{archivo}: {directorio}"
