"""Flujo de ci/trivy/ejecutar-trivy.sh y ci/desplegar-imagen-analizada.sh con un `docker` SIMULADO (resultados controlados).

El `docker` falso registra cada invocación en un archivo: así se comprueba qué comandos se ejecutaron y en qué orden (p. ej. que tras un
bloqueo o un error técnico no ocurre ningún `compose`) y que la imagen desplegada es la analizada. El evaluador es el REAL; el escáner no:
esto no es un escaneo real ni una ejecución de Jenkins.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci.ayudantes_trivy import ID_CONFIG, IMAGEN_REF, VERSION, reporte, tar_de_imagen, vulnerabilidad

RAIZ = Path(__file__).resolve().parents[2]
SH = shutil.which("sh")
HTML = "trivy-report.html"
JSON = "trivy-report.json"
pytestmark = pytest.mark.skipif(SH is None, reason="se necesita un intérprete sh")

DOCKER_FALSO = r"""#!/bin/sh
echo "$*" >> "$FAKE_LOG"
# Volúmenes simulados: un directorio por volumen en $FAKE_VOLS; el contenedor auxiliar apunta a los suyos con archivos _aux_<nombre>.<punto>.
sha() { "$FAKE_PYTHON" -c "import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest(), sys.argv[1])" "$1"; }
destino_en_aux() {
    aux="${1%%:*}"; ruta="${1#*:}"; punto="${ruta#/}"; punto="${punto%%/*}"
    vol="$(cat "$FAKE_VOLS/_aux_$aux.$punto")"
    resto="${ruta#/$punto}"
    DIR_AUX="$FAKE_VOLS/$vol$resto"
}
case "$1" in
volume)
    case "$2" in
        create) mkdir -p "$FAKE_VOLS/$3" ;;
        rm) shift 2; for v in "$@"; do case "$v" in -f) ;; *) rm -rf "$FAKE_VOLS/$v" ;; esac; done ;;
    esac
    exit 0 ;;
create)
    [ -n "${FAKE_CREATE_FALLA:-}" ] && exit 1
    nombre=""; previo=""
    for a in "$@"; do
        [ "$previo" = "--name" ] && nombre="$a"
        if [ "$previo" = "-v" ]; then
            case "$a" in *:/trabajo) echo "${a%%:/trabajo}" > "$FAKE_VOLS/_aux_$nombre.trabajo" ;; *:/resultados) echo "${a%%:/resultados}" > "$FAKE_VOLS/_aux_$nombre.resultados" ;; esac
        fi
        previo="$a"
    done
    exit 0 ;;
rm)
    rm -f "$FAKE_VOLS"/_aux_"$3".*
    exit 0 ;;
cp)
    case "$2" in
        *:/resultados/.)
            [ -n "${FAKE_RECUPERAR_FALLA:-}" ] && exit 1
            destino_en_aux "${2%/.}"
            cp -R "$DIR_AUX/." "$3/"
            exit 0 ;;
    esac
    [ -n "${FAKE_CP_FALLA:-}" ] && exit 1
    destino_en_aux "$3"
    if [ -n "${FAKE_TAR_CORRUPTO:-}" ] && [ "${3##*/}" = "imagen.tar" ]; then : > "$DIR_AUX"; else cp "$2" "$DIR_AUX"; fi
    exit 0 ;;
image)
    [ -n "${FAKE_SIN_IMAGEN:-}" ] && exit 1
    printf '%s\n' "$FAKE_IMAGE_ID"
    exit 0 ;;
save)
    [ -n "${FAKE_SAVE_FALLA:-}" ] && exit 1
    cp "$FAKE_TAR" "$3"
    exit 0 ;;
compose)
    echo "BACKEND_IMAGE=${BACKEND_IMAGE:-}" >> "$FAKE_LOG"
    case "$*" in *" ps -q"*) printf '%s\n' "abc123" ;; esac
    exit 0 ;;
inspect)
    printf '%s\n' "$FAKE_IMAGEN_EN_USO"
    exit 0 ;;
run) ;;
*) exit 0 ;;
esac
shift
trabajo=""; resultados=""; previo=""
for a in "$@"; do
    if [ "$previo" = "-v" ]; then
        case "$a" in
            *:/trabajo | *:/trabajo:ro) trabajo="$FAKE_VOLS/${a%%:/trabajo*}" ;;
            *:/resultados) resultados="$FAKE_VOLS/${a%%:/resultados}" ;;
        esac
    fi
    previo="$a"
done
case " $* " in
    *" --entrypoint sha256sum "*) sha - ;;
    *'sha256sum "$f"'*)
        [ -s "$trabajo/imagen.tar" ] || exit 1
        sha /trabajo/imagen.tar < "$trabajo/imagen.tar"
        exit 0 ;;
    *"trivy version --format json"*)
        [ -n "${FAKE_VERSION_FALLA:-}" ] && exit 1
        cp "$FAKE_VERSION" "$trabajo/trivy-version.json"
        exit 0 ;;
    *" --entrypoint sh "*) exit 0 ;;
    *" image --input "*)
        [ -n "${FAKE_SCAN_FALLA:-}" ] && exit 1
        cp "$FAKE_CRUDO" "$trabajo/trivy-crudo.json"
        exit 0 ;;
    *" convert "*)
        [ -n "${FAKE_CONVERT_FALLA:-}" ] && exit 1
        printf '<html>informe</html>' > "$resultados/trivy-report.html"
        exit 0 ;;
    *evaluar_trivy.py*)
        flag=0
        for a in "$@"; do
            if [ "$flag" = 0 ]; then
                if [ "$a" = "/trabajo/evaluar_trivy.py" ]; then flag=1; set --; fi
            else
                case "$a" in
                    /trabajo/*) a="$trabajo/${a#/trabajo/}" ;;
                    /resultados/*) a="$resultados/${a#/resultados/}" ;;
                esac
                set -- "$@" "$a"
            fi
        done
        exec "$FAKE_PYTHON" -I "$trabajo/evaluar_trivy.py" "$@" ;;
esac
exit 0
"""


class Entorno:
    def __init__(self, carpeta):
        self.trabajo = carpeta / "agente"
        self.trabajo.mkdir()
        shutil.copytree(RAIZ / "ci", self.trabajo / "ci")
        binarios = carpeta / "bin"
        binarios.mkdir()
        falso = binarios / "docker"
        falso.write_text(DOCKER_FALSO, encoding="utf-8", newline="\n")
        falso.chmod(falso.stat().st_mode | stat.S_IEXEC)
        self.registro = carpeta / "docker.log"
        self.registro.write_text("", encoding="utf-8")
        self.volumenes = carpeta / "volumenes"
        self.volumenes.mkdir()
        tar_de_imagen(carpeta / "imagen.tar")
        (carpeta / "version.json").write_text(json.dumps(VERSION), encoding="utf-8")
        self.variables = {
            **{k: v for k, v in os.environ.items() if k != "MSYS_NO_PATHCONV"},
            "PATH": f"{binarios}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.registro), "FAKE_VOLS": str(self.volumenes), "BUILD_NUMBER": "7", "FAKE_IMAGE_ID": ID_CONFIG, "FAKE_TAR": str(carpeta / "imagen.tar"),
            "FAKE_VERSION": str(carpeta / "version.json"), "FAKE_PYTHON": sys.executable, "FAKE_CRUDO": str(carpeta / "crudo.json"),
            "FAKE_IMAGEN_EN_USO": ID_CONFIG, "IMAGEN_REF": IMAGEN_REF, "IMAGEN_ID": ID_CONFIG, "AMBIENTE": "qa",
            "COMPOSE_PROJECT": "proyecto-prueba",
        }

    def con_reporte(self, datos):
        Path(self.variables["FAKE_CRUDO"]).write_text(json.dumps(datos), encoding="utf-8")
        return self

    def ejecutar(self, script, **extra):
        entorno = {**self.variables, **extra}
        return subprocess.run([SH, script], cwd=self.trabajo, env=entorno, capture_output=True, text=True, timeout=120, check=False)

    def trivy(self, **extra):
        return self.ejecutar("ci/trivy/ejecutar-trivy.sh", **extra)

    def desplegar(self, **extra):
        return self.ejecutar("ci/desplegar-imagen-analizada.sh", **extra)

    def comandos(self):
        return self.registro.read_text(encoding="utf-8").splitlines()

    def resultado(self, nombre):
        return self.trabajo / "trivy-resultados" / nombre

    def resumen(self):
        return self.resultado("resumen-trivy.txt").read_text(encoding="utf-8")


@pytest.fixture
def entorno(tmp_path):
    return Entorno(tmp_path)


def sin_compose(entorno):
    return not any(linea.startswith("compose") for linea in entorno.comandos())


# ============================================ política: LOW permite; MEDIUM, HIGH y CRITICAL bloquean ============================================

def test_solo_low_aprueba_y_genera_los_tres_reportes(entorno):
    proceso = entorno.con_reporte(reporte([vulnerabilidad("LOW")])).trivy()
    assert proceso.returncode == 0, proceso.stdout + proceso.stderr
    for nombre in (HTML, JSON, "resumen-trivy.txt"):
        assert entorno.resultado(nombre).is_file(), nombre
    assert "Veredicto: APROBADO" in entorno.resumen()
    assert not (entorno.trabajo / "trivy-trabajo").exists(), "el tar de la imagen no queda en el workspace"


@pytest.mark.parametrize("severidad", ["MEDIUM", "HIGH", "CRITICAL"])
def test_medium_high_y_critical_bloquean_pero_el_reporte_se_conserva(entorno, severidad):
    proceso = entorno.con_reporte(reporte([vulnerabilidad("LOW"), vulnerabilidad(severidad, "CVE-2026-0042")])).trivy()
    assert proceso.returncode == 1
    assert "Veredicto: BLOQUEADO" in entorno.resumen()
    assert "CVE-2026-0042" in entorno.resumen()
    assert entorno.resultado(HTML).is_file() and entorno.resultado(JSON).is_file()
    assert sin_compose(entorno)


def test_el_analisis_usa_la_imagen_fijada_el_guardado_por_id_y_un_solo_escaneo(entorno):
    entorno.con_reporte(reporte()).trivy()
    comandos = entorno.comandos()
    assert f"save -o trivy-trabajo/imagen.tar {ID_CONFIG}" in comandos
    escaneos = [c for c in comandos if " image --input " in c]
    assert len(escaneos) == 1
    assert "ghcr.io/aquasecurity/trivy:0.76.1@sha256:" in escaneos[0]
    assert "--ignore-unfixed" not in escaneos[0] and "--severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL" in escaneos[0]


# ============================================ el error técnico bloquea y no aparenta un análisis exitoso ============================================

def comprobar_error_tecnico(entorno, proceso, motivo):
    assert proceso.returncode == 2, proceso.stdout + proceso.stderr
    resumen = entorno.resumen()
    assert "ERROR TÉCNICO" in resumen and motivo in resumen
    assert "APROBADO" not in resumen
    assert not entorno.resultado(HTML).exists()
    assert not entorno.resultado(JSON).exists()
    assert entorno.resultado("consola-trivy.log").is_file(), "el diagnóstico se conserva"
    assert sin_compose(entorno)


def test_si_trivy_no_puede_analizar_es_error_tecnico(entorno):
    proceso = entorno.con_reporte(reporte()).trivy(FAKE_SCAN_FALLA="1")
    comprobar_error_tecnico(entorno, proceso, "Trivy no pudo completar el análisis")


def test_si_no_hay_informacion_de_la_base_es_error_tecnico(entorno):
    comprobar_error_tecnico(entorno, entorno.con_reporte(reporte()).trivy(FAKE_VERSION_FALLA="1"), "información de su base")


def test_si_no_se_puede_exportar_la_imagen_es_error_tecnico(entorno):
    comprobar_error_tecnico(entorno, entorno.con_reporte(reporte()).trivy(FAKE_SAVE_FALLA="1"), "docker save")


def test_si_la_imagen_no_existe_es_error_tecnico(entorno):
    comprobar_error_tecnico(entorno, entorno.con_reporte(reporte()).trivy(FAKE_SIN_IMAGEN="1"), "no existe en el agente")


def test_si_la_referencia_ya_no_apunta_al_id_registrado_es_error_tecnico(entorno):
    proceso = entorno.con_reporte(reporte()).trivy(FAKE_IMAGE_ID="sha256:" + "9" * 64)
    comprobar_error_tecnico(entorno, proceso, "ya no apunta al ID registrado")


def test_si_falla_el_html_es_error_tecnico_y_no_deja_reportes(entorno):
    comprobar_error_tecnico(entorno, entorno.con_reporte(reporte()).trivy(FAKE_CONVERT_FALLA="1"), "trivy-report.html")


def test_un_resultado_de_otra_imagen_es_error_tecnico(entorno):
    datos = reporte()
    datos["Metadata"]["ImageID"] = "sha256:" + "f" * 64
    comprobar_error_tecnico(entorno, entorno.con_reporte(datos).trivy(), "ImageID distinto")


def test_un_archivo_de_ignorados_detiene_el_analisis_sin_ejecutarlo(entorno):
    (entorno.trabajo / ".trivyignore").write_text("CVE-2026-0001\n", encoding="utf-8")
    proceso = entorno.con_reporte(reporte()).trivy()
    comprobar_error_tecnico(entorno, proceso, "podrían suprimir hallazgos")
    assert not any(" image --input " in c for c in entorno.comandos())


# ============================================ transferencia por volúmenes (Jenkins dentro de un contenedor) ============================================

def indice(comandos, fragmento):
    return next(i for i, c in enumerate(comandos) if fragmento in c)


def volumenes_de_la_ejecucion(entorno):
    return sorted(p.name for p in entorno.volumenes.iterdir() if p.name.startswith(("igualab-trivy-trabajo-", "igualab-trivy-resultados-", "_aux_")))


def test_el_tar_y_los_resultados_viajan_por_volumenes_nombrados_sin_rutas_del_agente(entorno):
    entorno.con_reporte(reporte()).trivy()
    montajes = [parte for c in entorno.comandos() for anterior, parte in zip(c.split(), c.split()[1:]) if anterior == "-v"]
    assert montajes
    for montaje in montajes:
        assert re.match(r"^igualab-trivy-(trabajo|resultados|cache)-[a-z0-9-]+:/(trabajo|resultados|cache)(:ro)?$", montaje), montaje
    assert str(entorno.trabajo) not in "\n".join(entorno.comandos())


def test_el_tar_se_copia_y_se_verifica_desde_un_contenedor_antes_de_analizar(entorno):
    entorno.con_reporte(reporte()).trivy()
    comandos = entorno.comandos()
    copia = indice(comandos, "cp trivy-trabajo/imagen.tar igualab-trivy-aux-qa-7-")
    verificacion = indice(comandos, 'sha256sum "$f"')
    assert "[ -f \"$f\" ] && [ -r \"$f\" ] && [ -s \"$f\" ]" in comandos[verificacion]
    assert "--user " in comandos[verificacion] and ":/trabajo:ro" in comandos[verificacion]
    assert copia < verificacion < indice(comandos, " image --input ")


def test_si_el_tar_llega_vacio_al_volumen_no_se_analiza(entorno):
    proceso = entorno.con_reporte(reporte()).trivy(FAKE_TAR_CORRUPTO="1")
    comprobar_error_tecnico(entorno, proceso, "imagen.tar no existe, no es legible o está vacío")
    assert not any(" image --input " in c for c in entorno.comandos())


def test_si_no_se_puede_copiar_el_tar_al_volumen_es_error_tecnico(entorno):
    comprobar_error_tecnico(entorno, entorno.con_reporte(reporte()).trivy(FAKE_CP_FALLA="1"), "copiar imagen.tar al volumen")
    assert not any(" image --input " in c for c in entorno.comandos())


def test_si_no_se_pueden_recuperar_los_resultados_es_error_tecnico_aunque_el_analisis_apruebe(entorno):
    proceso = entorno.con_reporte(reporte([vulnerabilidad("LOW")])).trivy(FAKE_RECUPERAR_FALLA="1")
    comprobar_error_tecnico(entorno, proceso, "recuperar los resultados")


@pytest.mark.parametrize("extra, codigo", [
    ({}, 0),
    ({"FAKE_SCAN_FALLA": "1"}, 2),
    ({"FAKE_TAR_CORRUPTO": "1"}, 2),
    ({"FAKE_CP_FALLA": "1"}, 2),
    ({"FAKE_RECUPERAR_FALLA": "1"}, 2),
], ids=["aprobada", "falla-trivy", "tar-vacio", "falla-copia", "falla-recuperacion"])
def test_se_eliminan_los_recursos_de_la_ejecucion_con_cualquier_resultado(entorno, extra, codigo):
    assert entorno.con_reporte(reporte()).trivy(**extra).returncode == codigo
    assert volumenes_de_la_ejecucion(entorno) == []
    assert not (entorno.trabajo / "trivy-trabajo").exists()
    assert any(c.startswith("rm -f igualab-trivy-aux-qa-7-") for c in entorno.comandos())


def test_bloqueada_elimina_los_recursos_y_deja_los_reportes_en_el_workspace(entorno):
    proceso = entorno.con_reporte(reporte([vulnerabilidad("HIGH", "CVE-2026-0042")])).trivy()
    assert proceso.returncode == 1
    assert volumenes_de_la_ejecucion(entorno) == []
    assert all(entorno.resultado(n).is_file() for n in (HTML, JSON, "resumen-trivy.txt", "consola-trivy.log"))


def test_solo_se_eliminan_los_volumenes_de_esa_ejecucion_no_la_cache_de_la_base(entorno):
    entorno.con_reporte(reporte()).trivy()
    borrados = " ".join(c for c in entorno.comandos() if c.startswith("volume rm"))
    assert "igualab-trivy-trabajo-qa-7-" in borrados and "igualab-trivy-resultados-qa-7-" in borrados
    assert "cache" not in borrados
    assert (entorno.volumenes / "igualab-trivy-cache-qa").is_dir()


# ============================================ despliegue: exactamente la imagen analizada ============================================

def test_el_despliegue_usa_la_imagen_analizada_sin_construir_ni_descargar(entorno):
    proceso = entorno.desplegar()
    assert proceso.returncode == 0, proceso.stdout + proceso.stderr
    comandos = entorno.comandos()
    bajar = comandos.index("compose -p proyecto-prueba down")
    subir = comandos.index("compose -p proyecto-prueba up -d --no-build --pull never")
    assert bajar < subir
    assert f"BACKEND_IMAGE={IMAGEN_REF}" in comandos
    assert not any("build" in c.replace("--no-build", "") for c in comandos)


def test_si_la_referencia_apunta_a_otra_imagen_no_se_despliega(entorno):
    proceso = entorno.desplegar(FAKE_IMAGE_ID="sha256:" + "9" * 64)
    assert proceso.returncode == 1
    assert sin_compose(entorno), "no se detiene ni se reemplaza el servicio actual"


def test_si_la_imagen_analizada_ya_no_existe_no_se_despliega(entorno):
    proceso = entorno.desplegar(FAKE_SIN_IMAGEN="1")
    assert proceso.returncode == 1
    assert sin_compose(entorno)


def test_si_el_contenedor_no_usa_la_imagen_analizada_el_despliegue_falla(entorno):
    proceso = entorno.desplegar(FAKE_IMAGEN_EN_USO="sha256:" + "8" * 64)
    assert proceso.returncode == 1
    assert "no la imagen analizada" in proceso.stderr
