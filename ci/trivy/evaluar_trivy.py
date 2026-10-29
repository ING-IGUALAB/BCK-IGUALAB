"""Evalúa el resultado de Trivy para el stage «Trivy» del Jenkinsfile. Solo biblioteca estándar.

Un MISMO reporte JSON de Trivy sirve para tres cosas: validar que el análisis es completo y corresponde a la imagen, decidir el
bloqueo y producir el resumen. El HTML se genera después con `trivy convert` a partir del JSON saneado que escribe este programa.

Política del TA (sin excepciones ni mecanismos de omisión):
* LOW: permitido.
* MEDIUM, HIGH y CRITICAL: bloquean, tengan o no versión corregida.
* UNKNOWN: pendiente de clasificación; NO es una aprobación automática, así que también bloquea.

Códigos de salida: 0 aprobado · 10 bloqueado por vulnerabilidades · 20 error técnico (el análisis no es válido o está incompleto).
Un error técnico nunca se interpreta como «sin vulnerabilidades».
"""
import argparse
import json
import re
import sys
import tarfile
from collections import Counter
from pathlib import Path

APROBADO = 0
BLOQUEADO = 10
ERROR_TECNICO = 20

SEVERIDADES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
SEVERIDADES_QUE_BLOQUEAN = ("CRITICAL", "HIGH", "MEDIUM", "UNKNOWN")  # solo LOW queda permitido
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_BLOQUEANTES_EN_RESUMEN = 60
SIN_DATO = "(sin dato)"

POLITICA = (
    "LOW permitido · MEDIUM, HIGH y CRITICAL bloquean (aunque no tengan versión corregida) · UNKNOWN queda pendiente de "
    "clasificación y también bloquea · sin --ignore-unfixed, .trivyignore, VEX ni exclusiones de paquetes o rutas"
)


class ErrorTecnico(Exception):
    """El análisis no se pudo completar o su resultado no es confiable."""


def leer_json(ruta: Path, que: str) -> dict:
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ErrorTecnico(f"{que} ausente o ilegible ({type(error).__name__})") from error
    if not isinstance(datos, dict):
        raise ErrorTecnico(f"{que} no tiene la forma esperada")
    return datos


def digest_de_configuracion(ruta_tar: Path) -> tuple[str, str | None]:
    """(digest de la configuración, digest del índice OCI si existe) de un `docker save` de una sola imagen."""
    try:
        with tarfile.open(ruta_tar) as tar:
            manifiesto = json.load(tar.extractfile("manifest.json"))
            indice = _leer_indice_oci(tar)
    except (OSError, KeyError, ValueError, tarfile.TarError, TypeError) as error:
        raise ErrorTecnico(f"no se pudo leer el manifiesto del tar de la imagen ({type(error).__name__})") from error
    if not isinstance(manifiesto, list) or len(manifiesto) != 1:
        raise ErrorTecnico("el tar debe contener exactamente una imagen")
    ruta_config = str(manifiesto[0].get("Config", ""))
    nombre = ruta_config.rsplit("/", 1)[-1].removesuffix(".json")
    digest = f"sha256:{nombre}"
    if not DIGEST.match(digest):
        raise ErrorTecnico("el manifiesto del tar no declara el digest de la configuración")
    return digest, indice


def _leer_indice_oci(tar: tarfile.TarFile) -> str | None:
    try:
        indice = json.load(tar.extractfile("index.json"))
    except KeyError:
        return None  # almacén clásico de Docker: el ID de la imagen es el digest de su configuración
    manifiestos = indice.get("manifests") or []
    return manifiestos[0].get("digest") if len(manifiestos) == 1 else None


def comprobar_identidad(id_docker: str, id_config: str, id_indice: str | None) -> None:
    """El tar analizado es la imagen registrada: su ID de Docker es el digest de configuración (almacén clásico) o del índice OCI."""
    if id_docker not in (id_config, id_indice):
        raise ErrorTecnico("el tar analizado no corresponde al ID de la imagen que se desplegaría")


def validar_reporte(reporte: dict, id_config: str) -> None:
    if reporte.get("SchemaVersion") != 2:
        raise ErrorTecnico("versión de esquema de Trivy no reconocida")
    if reporte.get("ArtifactType") != "container_image":
        raise ErrorTecnico("el reporte no es de una imagen de contenedor")
    metadatos = reporte.get("Metadata") or {}
    if metadatos.get("ImageID") != id_config:
        raise ErrorTecnico("el reporte no corresponde a la imagen analizada (ImageID distinto)")
    if not (metadatos.get("OS") or {}).get("Family"):
        raise ErrorTecnico("Trivy no identificó el sistema operativo de la imagen")
    if reporte.get("ExperimentalModifiedFindings"):
        raise ErrorTecnico("el reporte trae hallazgos suprimidos (VEX o ignorados): la política no admite supresiones")
    resultados = reporte.get("Results")
    if not isinstance(resultados, list):
        raise ErrorTecnico("el reporte no trae resultados")
    if not _paquetes_analizados(resultados, "os-pkgs"):
        raise ErrorTecnico("no se analizaron paquetes del sistema operativo")
    if not _paquetes_analizados(resultados, "lang-pkgs"):
        raise ErrorTecnico("no se analizaron dependencias de aplicación")


def _paquetes_analizados(resultados: list, clase: str) -> int:
    return sum(len(r.get("Packages") or []) for r in resultados if r.get("Class") == clase)


def normalizar_severidad(valor) -> str:
    texto = str(valor or "").upper()
    return texto if texto in SEVERIDADES else "UNKNOWN"


def hallazgos(reporte: dict) -> list[dict]:
    lista = []
    for resultado in reporte.get("Results") or []:
        for v in resultado.get("Vulnerabilities") or []:
            lista.append({
                "severidad": normalizar_severidad(v.get("Severity")),
                "cve": v.get("VulnerabilityID", "?"),
                "paquete": v.get("PkgName", "?"),
                "instalada": v.get("InstalledVersion", "?"),
                "corregida": v.get("FixedVersion") or "sin versión corregida",
                "clase": resultado.get("Class", "?"),
            })
    return lista


def contar(lista: list[dict]) -> Counter:
    cuenta = Counter({s: 0 for s in SEVERIDADES})
    cuenta.update(h["severidad"] for h in lista)
    return cuenta


def bloqueantes_ordenados(lista: list[dict]) -> list[dict]:
    orden = {s: n for n, s in enumerate(SEVERIDADES)}
    bloqueantes = [h for h in lista if h["severidad"] in SEVERIDADES_QUE_BLOQUEAN]
    return sorted(bloqueantes, key=lambda h: (orden[h["severidad"]], h["paquete"], h["cve"]))


def motivo_de_bloqueo(cuenta: Counter) -> str:
    partes = [f"{cuenta[s]} {s}" for s in SEVERIDADES_QUE_BLOQUEAN if cuenta[s]]
    return "hallazgos que bloquean el despliegue: " + ", ".join(partes)


def sanear(reporte: dict, imagen_ref: str) -> dict:
    """Copia del reporte sin la configuración de la imagen (variables de entorno, historial) y con la imagen real como nombre."""
    copia = json.loads(json.dumps(reporte))
    copia["ArtifactName"] = imagen_ref
    (copia.get("Metadata") or {}).pop("ImageConfig", None)
    return copia


def construir_resumen(datos: dict) -> str:
    lineas = [
        "RESUMEN TRIVY · Igualab",
        "",
        f"Veredicto: {datos['veredicto']}",
        f"Motivo: {datos['motivo']}",
        "",
        f"Imagen: {datos.get('imagen_ref', SIN_DATO)}",
        f"ID de la imagen (Docker): {datos.get('imagen_id', SIN_DATO)}",
        f"Digest de configuración (el ImageID de Trivy): {datos.get('id_config', SIN_DATO)}",
        f"Trivy: {datos.get('trivy_version', SIN_DATO)} · analizador: {datos.get('trivy_imagen', SIN_DATO)}",
        f"Base de vulnerabilidades: {datos.get('base', SIN_DATO)}",
        f"Sistema operativo: {datos.get('sistema', SIN_DATO)}",
        f"Alcance: {datos.get('alcance', SIN_DATO)}",
        f"Política: {POLITICA}",
    ]
    cuenta = datos.get("cuenta")
    if cuenta is not None:
        lineas += ["", "Hallazgos por severidad: " + " · ".join(f"{s} {cuenta[s]}" for s in SEVERIDADES)]
        lineas.append(f"Hallazgos permitidos (LOW): {cuenta['LOW']} · CVE distintos en total: {datos['cve_distintos']}")
    lineas += _detalle_de_bloqueantes(datos.get("bloqueantes") or [])
    lineas += ["", "Reporte completo: trivy-report.html y trivy-report.json (mismos datos)."]
    return "\n".join(lineas) + "\n"


def _detalle_de_bloqueantes(bloqueantes: list[dict]) -> list[str]:
    if not bloqueantes:
        return []
    lineas = ["", "Hallazgos bloqueantes (severidad · CVE · paquete · instalada → corregida):"]
    for h in bloqueantes[:MAX_BLOQUEANTES_EN_RESUMEN]:
        lineas.append(f"  {h['severidad']} · {h['cve']} · {h['paquete']} · {h['instalada']} → {h['corregida']}")
    restantes = len(bloqueantes) - MAX_BLOQUEANTES_EN_RESUMEN
    if restantes > 0:
        lineas.append(f"  … y {restantes} más (ver trivy-report.json).")
    return lineas


def descripcion_de_base(version: dict) -> str:
    base = version.get("VulnerabilityDB")
    if not isinstance(base, dict) or not base.get("UpdatedAt"):
        raise ErrorTecnico("no se pudo obtener la información de la base de vulnerabilidades")
    return (
        f"versión {base.get('Version', '?')} · generada {base['UpdatedAt']} · descargada {base.get('DownloadedAt', '?')} · "
        f"próxima actualización {base.get('NextUpdate', '?')}"
    )


def descripcion_de_sistema(reporte: dict) -> str:
    sistema = reporte["Metadata"]["OS"]
    fin_de_soporte = "sí" if sistema.get("EOSL") else "no"
    return f"{sistema.get('Family')} {sistema.get('Name', '?')} (fuera de soporte: {fin_de_soporte})"


def evaluar(argumentos: argparse.Namespace) -> int:
    datos = {
        "imagen_ref": argumentos.imagen_ref, "imagen_id": argumentos.imagen_id, "trivy_imagen": argumentos.trivy_imagen,
    }
    resumen = Path(argumentos.resumen)
    try:
        codigo = _evaluar(argumentos, datos)
    except ErrorTecnico as error:
        datos.update(veredicto="ERROR TÉCNICO (el análisis NO es válido; no se aprueba el despliegue)", motivo=str(error))
        resumen.write_text(construir_resumen(datos), encoding="utf-8")
        return ERROR_TECNICO
    resumen.write_text(construir_resumen(datos), encoding="utf-8")
    return codigo


def _evaluar(argumentos: argparse.Namespace, datos: dict) -> int:
    if not DIGEST.match(argumentos.imagen_id):
        raise ErrorTecnico("el ID de la imagen registrado no es un digest sha256 válido")
    id_config, id_indice = digest_de_configuracion(Path(argumentos.tar))
    comprobar_identidad(argumentos.imagen_id, id_config, id_indice)
    datos["id_config"] = id_config

    reporte = leer_json(Path(argumentos.crudo), "reporte de Trivy")
    version = leer_json(Path(argumentos.version_trivy), "versión de Trivy")
    validar_reporte(reporte, id_config)
    datos["trivy_version"] = version.get("Version") or (reporte.get("Trivy") or {}).get("Version") or "(desconocida)"
    datos["base"] = descripcion_de_base(version)
    datos["sistema"] = descripcion_de_sistema(reporte)
    resultados = reporte["Results"]
    datos["alcance"] = (
        f"{_paquetes_analizados(resultados, 'os-pkgs')} paquetes del sistema operativo y "
        f"{_paquetes_analizados(resultados, 'lang-pkgs')} dependencias de aplicación"
    )

    lista = hallazgos(reporte)
    cuenta = contar(lista)
    datos.update(cuenta=cuenta, cve_distintos=len({h["cve"] for h in lista}), bloqueantes=bloqueantes_ordenados(lista))
    Path(argumentos.saneado).write_text(
        json.dumps(sanear(reporte, argumentos.imagen_ref), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    if any(cuenta[s] for s in SEVERIDADES_QUE_BLOQUEAN):
        datos.update(veredicto="BLOQUEADO (no se despliega)", motivo=motivo_de_bloqueo(cuenta))
        return BLOQUEADO
    datos.update(veredicto="APROBADO", motivo=f"sin MEDIUM, HIGH, CRITICAL ni UNKNOWN; LOW permitido ({cuenta['LOW']})")
    return APROBADO


def leer_argumentos(argv: list[str]) -> argparse.Namespace:
    analizador = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for nombre in ("crudo", "version-trivy", "tar", "imagen-ref", "imagen-id", "trivy-imagen", "saneado", "resumen"):
        analizador.add_argument(f"--{nombre}", required=True)
    return analizador.parse_args(argv)


def main(argv: list[str]) -> int:
    return evaluar(leer_argumentos(argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
