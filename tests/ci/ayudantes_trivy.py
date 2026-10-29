"""Constructores de resultados CONTROLADOS de Trivy y de tars de imagen para las pruebas de ci/ (no son un escaneo real)."""
import io
import json
import tarfile

ID_CONFIG = "sha256:" + "a" * 64
ID_INDICE = "sha256:" + "b" * 64
IMAGEN_REF = "igualab-backend-qa:b7-0123456789ab"
TRIVY_IMAGEN = "ghcr.io/aquasecurity/trivy:0.76.1@sha256:" + "c" * 64
VERSION = {
    "Version": "0.76.1",
    "VulnerabilityDB": {"Version": 2, "UpdatedAt": "2026-10-28T01:21:08Z", "DownloadedAt": "2026-10-28T06:43:40Z", "NextUpdate": "2026-10-29T01:21:08Z"},
}


def vulnerabilidad(severidad, cve="CVE-2026-0001", paquete="libejemplo", corregida=None):
    datos = {"VulnerabilityID": cve, "PkgName": paquete, "InstalledVersion": "1.0", "Severity": severidad}
    if corregida:
        datos["FixedVersion"] = corregida
    return datos


def reporte(os_vulns=(), lang_vulns=(), **cambios):
    base = {
        "SchemaVersion": 2,
        "ArtifactName": "/trabajo/imagen.tar",
        "ArtifactType": "container_image",
        "Metadata": {"ImageID": ID_CONFIG, "OS": {"Family": "debian", "Name": "13.7"}, "ImageConfig": {"config": {"Env": ["SECRETO=valor"]}}},
        "Results": [
            {"Target": "imagen (debian 13.7)", "Class": "os-pkgs", "Type": "debian", "Packages": [{"Name": "libc6"}], "Vulnerabilities": list(os_vulns) or None},
            {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg", "Packages": [{"Name": "fastapi"}], "Vulnerabilities": list(lang_vulns) or None},
        ],
    }
    base.update(cambios)
    return base


def tar_de_imagen(ruta, con_indice=False, imagenes=1):
    manifiesto = [{"Config": f"blobs/sha256/{ID_CONFIG.removeprefix('sha256:')}", "Layers": []} for _ in range(imagenes)]
    miembros = {"manifest.json": manifiesto}
    if con_indice:
        miembros["index.json"] = {"schemaVersion": 2, "manifests": [{"digest": ID_INDICE}]}
    with tarfile.open(ruta, "w") as tar:
        for nombre, contenido in miembros.items():
            datos = json.dumps(contenido).encode()
            informacion = tarfile.TarInfo(nombre)
            informacion.size = len(datos)
            tar.addfile(informacion, io.BytesIO(datos))
