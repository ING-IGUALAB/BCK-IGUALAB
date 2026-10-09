"""Etapa 1 de ingesta: validaciones del archivo y de la empresa.

T03, T05–T08 y hash de T10. LTX:RF-012, RF-013, RN-018, RN-021, RN-022, RN-024;
RNF-007, RNF-011, RNF-015. Decisiones D07, D08, D09, D18, D19.
Nivel: unidad, HTTP de prueba y SQLite aislado. No prueba persistencia.
"""
import hashlib
import io
import uuid
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers, UploadFile

from app.exception_handlers import register_exception_handlers
from app.exceptions import BusinessValidationError, NotFoundError, PayloadTooLargeError
from app.models import Empresa, SectorEmpresa
from app.request_id import RequestIDMiddleware
from app.services.ingesta import validacion
from app.services.ingesta.reglas import TAMANO_MAXIMO_BYTES

BOM = b"\xef\xbb\xbf"


def lector(datos: bytes, solicitudes: list[int] | None = None):
    vista = memoryview(datos)
    posicion = 0

    async def leer(cantidad: int) -> bytes:
        nonlocal posicion
        if solicitudes is not None:
            solicitudes.append(cantidad)
        bloque = bytes(vista[posicion:posicion + cantidad])
        posicion += len(bloque)
        return bloque

    return leer


async def validar(datos: bytes, nombre: str = "memoria.md", **kwargs):
    return await validacion.validar_archivo(nombre, lector(datos), **kwargs)


async def rechazo(datos: bytes, nombre: str = "memoria.md", **kwargs):
    with pytest.raises((BusinessValidationError, PayloadTooLargeError)) as capturado:
        await validar(datos, nombre, **kwargs)
    return capturado.value


# --- T03: empresa existente, activa y sector derivado de la BD ---------------

@pytest.fixture
def db_empresas():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Empresa.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        activa = Empresa(nombre="Minera Activa", sector=SectorEmpresa.MINERIA, activa=True)
        inactiva = Empresa(nombre="Energía Inactiva", sector=SectorEmpresa.ENERGIA, activa=False)
        session.add_all([activa, inactiva])
        session.commit()
        db = SimpleNamespace(get=AsyncMock(side_effect=session.get))
        yield db, activa, inactiva
    engine.dispose()


async def test_empresa_activa_devuelve_sector_de_la_bd(db_empresas):
    db, activa, _ = db_empresas
    empresa = await validacion.obtener_empresa_activa(db, activa.id)
    assert empresa.id == activa.id
    assert empresa.sector is SectorEmpresa.MINERIA


async def test_sector_declarado_coincidente_es_aceptado(db_empresas):
    db, activa, _ = db_empresas
    empresa = await validacion.obtener_empresa_activa(db, activa.id, SectorEmpresa.MINERIA)
    assert empresa.id == activa.id


async def test_empresa_inexistente(db_empresas):
    db, _, _ = db_empresas
    with pytest.raises(NotFoundError) as capturado:
        await validacion.obtener_empresa_activa(db, uuid.uuid4())
    assert capturado.value.code == "COMPANY_NOT_FOUND"


async def test_empresa_inactiva(db_empresas):
    db, _, inactiva = db_empresas
    with pytest.raises(BusinessValidationError) as capturado:
        await validacion.obtener_empresa_activa(db, inactiva.id)
    assert capturado.value.code == "COMPANY_INACTIVE"


@pytest.mark.parametrize("sector", [SectorEmpresa.PETROLEO, SectorEmpresa.ENERGIA])
async def test_sector_declarado_incompatible(db_empresas, sector):
    db, activa, _ = db_empresas
    with pytest.raises(BusinessValidationError) as capturado:
        await validacion.obtener_empresa_activa(db, activa.id, sector)
    assert capturado.value.code == "COMPANY_SECTOR_MISMATCH"


# --- T05: tipo real del archivo; extensión y MIME no bastan ------------------

@pytest.mark.parametrize("nombre", ["memoria.md", "MEMORIA.MD", "reporte 2025 ñ.Md"])
async def test_extension_md_aceptada(nombre):
    assert (await validar(b"# Memoria\n", nombre)).nombre_archivo == nombre


@pytest.mark.parametrize("nombre", ["memoria.pdf", "memoria.txt", "memoria.md.exe", "memoria", "memoria.markdown"])
async def test_extension_no_permitida(nombre):
    assert (await rechazo(b"# Memoria\n", nombre)).code == "INVALID_FILE_TYPE"


@pytest.mark.parametrize("nombre", [None, "", "   ", "a" * 253 + ".md"])
async def test_nombre_de_archivo_invalido(nombre):
    assert (await rechazo(b"# Memoria\n", nombre)).code == "INVALID_FILE_NAME"


async def test_nombre_de_255_caracteres_aceptado():
    nombre = "a" * 252 + ".md"
    assert (await validar(b"# Memoria\n", nombre)).nombre_archivo == nombre


async def test_pdf_renombrado_como_md_con_mime_markdown_se_rechaza():
    pdf = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    upload = UploadFile(
        file=io.BytesIO(pdf),
        filename="memoria.md",
        headers=Headers({"content-type": "text/markdown"}),
    )
    with pytest.raises(BusinessValidationError) as capturado:
        await validacion.validar_archivo(upload.filename, upload.read)
    assert capturado.value.code == "INVALID_ENCODING"


@pytest.mark.parametrize(
    "binario",
    [
        b"PK\x03\x04\x14\x00\x00\x00",  # cabecera ZIP/DOCX, bytes ASCII válidos en UTF-8
        b"# Titulo\n\x00\x00\x00",
        b"texto\x1bcon escape",
        b"texto\x7fcon DEL",
    ],
)
async def test_binario_valido_en_utf8_se_rechaza_por_caracteres_de_control(binario):
    error = await rechazo(binario)
    assert error.code == "INVALID_FILE_CONTENT"
    assert set(error.details) == {"linea"}


async def test_control_informa_linea_sin_exponer_contenido():
    error = await rechazo(b"linea 1\nlinea 2\nsecreto\x00\n")
    assert error.details == {"linea": 3}
    assert "secreto" not in error.message


async def test_tabulacion_y_salto_de_pagina_permitidos():
    documento = await validar(b"# Pagina 1\n\tsangria\n\x0c# Pagina 2\n")
    assert "\x0c" in documento.texto


# --- T06: UTF-8, tildes, ñ, CRLF y BOM (D19) ---------------------------------

async def test_texto_espanol_y_crlf_se_preservan_exactamente():
    texto = "# Año 2025\r\nÑandú, acción, pingüino, € y «comillas».\r\n"
    documento = await validar(texto.encode("utf-8"))
    assert documento.texto == texto
    assert documento.contenido == texto.encode("utf-8")
    assert documento.tiene_bom is False


@pytest.mark.parametrize(
    ("datos", "posicion"),
    [
        (b"caf\xe9", 3),                 # Latin-1
        (b"ok \xc3", 3),                 # secuencia truncada
        (b"\xc0\xaf", 0),                # codificación sobrelarga
        (b"x\xed\xa0\x80", 1),           # surrogate codificado
        (b"\xff\xfeh\x00o\x00", 0),      # UTF-16 con BOM
    ],
)
async def test_utf8_invalido_se_rechaza_sin_reparar(datos, posicion):
    error = await rechazo(datos)
    assert error.code == "INVALID_ENCODING"
    assert error.details == {"posicion_byte": posicion}


async def test_bom_aceptado_omitido_al_interpretar_e_incluido_en_hash():
    cuerpo = "# Reporte de sostenibilidad\nÑ\n".encode("utf-8")
    con_bom = await validar(BOM + cuerpo)
    sin_bom = await validar(cuerpo)

    assert con_bom.tiene_bom is True
    assert con_bom.texto == sin_bom.texto
    assert not con_bom.texto.startswith("﻿")
    assert con_bom.contenido == BOM + cuerpo
    assert con_bom.tamano_bytes == len(BOM + cuerpo)
    assert con_bom.sha256 == hashlib.sha256(BOM + cuerpo).hexdigest()
    assert con_bom.sha256 != sin_bom.sha256


async def test_solo_se_omite_el_bom_inicial():
    documento = await validar(BOM + "a﻿b".encode("utf-8"))
    assert documento.texto == "a﻿b"


# --- T07: vacío (D18) y tamaño real (D07) ------------------------------------

@pytest.mark.parametrize(
    "datos",
    [b"", b" ", b"\t", b"\n", b"\r\n", b"  \t\r\n \n", BOM, BOM + b" \n\t\r\n"],
)
async def test_documento_vacio_o_solo_blancos_se_rechaza(datos):
    assert (await rechazo(datos)).code == "EMPTY_DOCUMENT"


async def test_limite_es_exactamente_50_000_000_bytes():
    assert TAMANO_MAXIMO_BYTES == 50_000_000


async def test_archivo_de_exactamente_50_000_000_bytes_es_aceptado():
    datos = b"a" * TAMANO_MAXIMO_BYTES
    documento = await validar(datos)
    assert documento.tamano_bytes == 50_000_000
    assert documento.sha256 == hashlib.sha256(datos).hexdigest()


async def test_archivo_de_50_000_001_bytes_es_rechazado_con_413():
    solicitudes: list[int] = []
    with pytest.raises(PayloadTooLargeError) as capturado:
        await validacion.leer_archivo_limitado(
            lector(b"a" * (TAMANO_MAXIMO_BYTES + 1), solicitudes)
        )
    assert capturado.value.code == "FILE_TOO_LARGE"
    assert capturado.value.details == {"tamano_maximo_bytes": 50_000_000}
    assert "50 000 000 bytes" in capturado.value.message
    assert sum(solicitudes) == TAMANO_MAXIMO_BYTES + 1


async def test_lectura_se_detiene_al_primer_byte_excedente():
    solicitudes: list[int] = []
    with pytest.raises(PayloadTooLargeError):
        await validacion.leer_archivo_limitado(
            lector(b"a" * 10_000, solicitudes), limite_bytes=100, tamano_bloque=64
        )
    assert solicitudes == [64, 37]


async def test_tamano_declarado_por_el_cliente_no_se_usa():
    # `size` simula un Content-Length / tamaño de parte falso.
    upload = UploadFile(file=io.BytesIO(b"# A\n" * 30), filename="memoria.md", size=10)
    with pytest.raises(PayloadTooLargeError):
        await validacion.validar_archivo(upload.filename, upload.read, limite_bytes=100)

    pequeno = UploadFile(file=io.BytesIO(b"# A\n"), filename="memoria.md", size=10**9)
    documento = await validacion.validar_archivo(pequeno.filename, pequeno.read, limite_bytes=100)
    assert documento.tamano_bytes == 4


async def test_tamano_se_valida_antes_que_la_codificacion():
    error = await rechazo(b"\xff" * 101, limite_bytes=100)
    assert isinstance(error, PayloadTooLargeError)


def test_rechazo_por_tamano_usa_contrato_413_uniforme():
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)

    # Ruta solo de prueba: el endpoint real de ingesta pertenece a la Etapa 6.
    @app.post("/archivo")
    async def recibir():
        await validacion.leer_archivo_limitado(lector(b"a" * 11), limite_bytes=10)

    with TestClient(app) as client:
        response = client.post("/archivo")

    assert response.status_code == 413
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == "FILE_TOO_LARGE"
    assert error["details"] == {"tamano_maximo_bytes": 10}
    assert error["request_id"] == response.headers["x-request-id"]


# --- T08: tablas pipes opcionales y estructura cuando existen (D09) ---------

async def test_documento_sin_tablas_es_aceptado():
    documento = await validar("# Memoria\n\nTexto sin tablas.\n".encode("utf-8"))
    assert documento.tablas == 0


async def test_tablas_validas_se_cuentan():
    texto = (
        "# Indicadores\n\n"
        "| Indicador | Valor | Unidad |\n"
        "|:----------|------:|:------:|\n"
        "| Agua      | 1 200 | m³     |\n"
        "| Energía \\| total | 3,5 | GWh |\n"
        "\n"
        "Otra tabla sin pipes externos:\n\n"
        "Año | Monto\n"
        "--- | ---\n"
        "2025 | 10\n"
    )
    assert (await validar(texto.encode("utf-8"))).tablas == 2


async def test_tabla_con_columnas_inconsistentes_se_acepta_con_diagnostico_y_posiciones():
    # Decisión 2026-10-07: ya no se rechaza el documento (antes INVALID_MARKDOWN_TABLE).
    texto = (
        "# Datos\n"
        "| A | B | C |\n"
        "|---|---|\n"
        "| 1 | 2 | 3 |\n"
        "| 4 | 5 |\n"
    )
    datos = texto.encode("utf-8")
    documento = await validar(datos)

    # Bytes, hash y texto exactos: nada se rellena ni se reordena.
    assert documento.contenido == datos
    assert documento.sha256 == hashlib.sha256(datos).hexdigest()
    assert documento.texto == texto
    diagnostico = documento.diagnostico_tablas
    assert (diagnostico.tablas, diagnostico.tablas_inconsistentes) == (1, 1)
    assert diagnostico.total_inconsistencias == 2
    assert [asdict(d) for d in diagnostico.detalles] == [
        {"linea": 3, "inicio": texto.index("|---|---|\n"), "linea_tabla": 2,
         "columnas_esperadas": 3, "columnas_encontradas": 2},
        {"linea": 5, "inicio": texto.index("| 4 | 5 |"), "linea_tabla": 2,
         "columnas_esperadas": 3, "columnas_encontradas": 2},
    ]
    # El offset apunta al inicio exacto de la fila dentro del texto interpretado.
    assert all(texto[d.inicio] == "|" for d in diagnostico.detalles)

    (advertencia,) = documento.advertencias
    assert advertencia.codigo == validacion.ADVERTENCIA_TABLAS_INCONSISTENTES
    assert advertencia.detalles["total_inconsistencias"] == 2
    assert advertencia.detalles["inconsistencias"][0]["linea"] == 3


async def test_tablas_bien_formadas_no_generan_advertencia():
    texto = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    documento = await validar(texto.encode("utf-8"))
    assert documento.advertencias == ()
    assert documento.diagnostico_tablas.total_inconsistencias == 0
    assert documento.diagnostico_tablas.detalles == ()


async def test_offsets_de_inconsistencias_con_bom_y_crlf_son_sobre_el_texto_sin_bom():
    texto = "Intro\r\n\r\n| A | B |\r\n|---|---|\r\n| solo |\r\n"
    documento = await validar(BOM + texto.encode("utf-8"))
    assert documento.tiene_bom and documento.texto == texto
    (detalle,) = documento.diagnostico_tablas.detalles
    assert detalle.linea == 5 and detalle.inicio == texto.index("| solo |")
    assert documento.contenido.startswith(BOM)  # bytes originales intactos


async def test_inconsistencias_de_tabla_reportadas_se_limitan_sin_rechazar():
    filas = "".join("| x |\n" for _ in range(30))
    documento = await validar(("| A | B |\n|---|---|\n" + filas).encode("utf-8"))
    diagnostico = documento.diagnostico_tablas
    assert diagnostico.total_inconsistencias == 30
    assert len(diagnostico.detalles) == validacion.MAX_ERRORES_TABLA_REPORTADOS


async def test_tablas_dentro_de_bloques_de_codigo_se_ignoran():
    texto = (
        "```markdown\n"
        "| A | B |\n"
        "|---|\n"
        "```\n"
        "~~~~\n"
        "| X |\n"
        "|---|---|\n"
        "~~~\n"
        "~~~~\n"
    )
    assert (await validar(texto.encode("utf-8"))).tablas == 0


async def test_pipes_sueltos_y_lineas_horizontales_no_son_tablas():
    texto = "Uso de a | b en texto.\n\n---\n\nTítulo\n---\n    | código | indentado |\n    |---|\n"
    assert (await validar(texto.encode("utf-8"))).tablas == 0


async def test_tabla_termina_en_linea_sin_pipe():
    texto = "| A | B |\n|---|---|\n| 1 | 2 |\nPárrafo posterior.\n"
    assert (await validar(texto.encode("utf-8"))).tablas == 1


# --- Nombre del archivo: metadata, nunca una ruta de almacenamiento ------------

@pytest.mark.parametrize(
    "nombre",
    [
        "Memoria anual 2025.md",
        "Reporte de sostenibilidad GRI 2024.md",
        "Año ñandú acción – versión final.MD",
        "memoria (final) v2 [revisada].md",
        "informe.2025.preliminar.md",
        "ñ.md",
        "  con espacios al borde .md",
        "-guion-inicial.md",
        ".md.md",
        "..md",           # sin separadores: no escapa de ningún directorio
        "memoria...md",
    ],
)
async def test_nombres_habituales_con_espacios_y_unicode_son_validos(nombre):
    assert validacion.validar_nombre_archivo(nombre) == nombre
    assert (await validar(b"# Memoria\n", nombre)).nombre_archivo == nombre


@pytest.mark.parametrize(
    "nombre",
    [
        "../../x.md",
        "../x.md",
        "..\\..\\x.md",
        "a/b.md",
        "a\\b.md",
        "C:\\docs\\x.md",
        "/etc/passwd.md",
        "\\\\servidor\\recurso\\x.md",
        "x.md/",
        "x.md\\",
        "carpeta/",
    ],
)
async def test_nombres_con_separadores_de_ruta_se_rechazan(nombre):
    assert (await rechazo(b"# Memoria\n", nombre)).code == "INVALID_FILE_NAME"


@pytest.mark.parametrize(
    "nombre",
    [
        "a\x00b.md",
        "a\tb.md",
        "a\nb.md",
        "a\rb.md",
        "a\r\nb.md",
        "a\x0bb.md",
        "a\x0cb.md",
        "a\x1bb.md",
        "a\x7fb.md",
        "a\x85b.md",                       # NEL (control C1)
        "a\N{LINE SEPARATOR}b.md",
        "a\N{PARAGRAPH SEPARATOR}b.md",
        "memoria.md\n",
        "\nmemoria.md",
        "memoria.md\x00.exe",
    ],
)
async def test_nombres_con_controles_o_saltos_de_linea_se_rechazan(nombre):
    error = await rechazo(b"# Memoria\n", nombre)
    assert error.code == "INVALID_FILE_NAME"
    assert error.details is None or error.details == {}


async def test_el_rechazo_de_nombre_no_repite_el_nombre_recibido():
    error = await rechazo(b"# Memoria\n", "../../secreto-interno.md")
    assert "secreto-interno" not in error.message
    assert "../" not in error.message


async def test_el_nombre_se_valida_antes_que_la_extension_y_la_extension_sigue_siendo_obligatoria():
    assert (await rechazo(b"# Memoria\n", "../x.pdf")).code == "INVALID_FILE_NAME"
    assert (await rechazo(b"# Memoria\n", "x.pdf")).code == "INVALID_FILE_TYPE"
    assert (await rechazo(b"# Memoria\n", "x.md ")).code == "INVALID_FILE_TYPE"


async def test_nombre_de_255_caracteres_con_unicode_aceptado_y_256_rechazado():
    valido = "ñ" * 252 + ".md"
    assert (await validar(b"# Memoria\n", valido)).nombre_archivo == valido
    assert (await rechazo(b"# Memoria\n", "ñ" * 253 + ".md")).code == "INVALID_FILE_NAME"


async def test_nombre_invalido_no_llega_a_leer_el_archivo():
    solicitudes: list[int] = []
    with pytest.raises(BusinessValidationError):
        await validacion.validar_archivo(
            "../../x.md", lector(b"# Memoria\n", solicitudes)
        )
    assert solicitudes == []


# --- Revisión de la Etapa 1 previa a la fragmentación (regresiones) ----------

@pytest.mark.parametrize("separador", ["\u2028", "\u2029", "\x85", "\x0c"])
async def test_separadores_unicode_dentro_de_una_celda_no_parten_la_fila(separador):
    # Antes se usaba str.splitlines(), que parte la fila y rechazaba la tabla.
    texto = f"| A | B |\n|---|---|\n| uno{separador}dos | tres |\n"
    assert (await validar(texto.encode("utf-8"))).tablas == 1


async def test_encabezado_pegado_a_una_tabla_no_es_una_fila():
    texto = "| A | B |\n|---|---|\n| 1 | 2 |\n## Uno | Dos | Tres\n"
    assert (await validar(texto.encode("utf-8"))).tablas == 1


async def test_linea_con_acentos_graves_en_linea_no_abre_un_bloque_de_codigo():
    texto = "```inline``` al inicio de la línea\n```md\n| A | B |\n|---|\n```\n"
    assert (await validar(texto.encode("utf-8"))).tablas == 0


async def test_cerca_con_informacion_no_cierra_un_bloque_de_codigo():
    texto = "```\n```python\n| A | B |\n|---|\n```\n"
    assert (await validar(texto.encode("utf-8"))).tablas == 0


async def test_tabla_posterior_a_un_bloque_cerrado_si_se_valida():
    texto = "```\ncódigo\n```\n\n| A | B |\n|---|\n"
    diagnostico = (await validar(texto.encode("utf-8"))).diagnostico_tablas
    assert (diagnostico.tablas, diagnostico.total_inconsistencias) == (1, 1)


async def test_prosa_con_pipes_y_pipes_escapados_no_producen_rechazo():
    texto = (
        "Opciones: a | b | c, según el caso.\n"
        "Otra línea con | un pipe suelto.\n\n"
        "| Indicador | Detalle |\n"
        "|---|---|\n"
        "| Energía \\| total | 3,5 \\| GWh |\n"
    )
    assert (await validar(texto.encode("utf-8"))).tablas == 1


@pytest.mark.parametrize("mime", ["application/pdf", "application/octet-stream", "image/png"])
async def test_markdown_valido_no_se_rechaza_por_el_mime_declarado(mime):
    upload = UploadFile(
        file=io.BytesIO(b"# Memoria\n\nTexto.\n"),
        filename="memoria.md",
        headers=Headers({"content-type": mime}),
    )
    documento = await validacion.validar_archivo(upload.filename, upload.read)
    assert documento.texto == "# Memoria\n\nTexto.\n"


async def test_no_se_exigen_encabezados_codigos_gri_sanciones_ni_tablas():
    texto = "Texto plano sin estructura, sin códigos ni menciones especiales.\n"
    documento = await validar(texto.encode("utf-8"))
    assert documento.tablas == 0
    assert documento.texto == texto


async def test_original_y_hash_no_se_alteran_con_crlf_y_controles_permitidos():
    original = BOM + "# Año\r\n\tsangría\r\n\x0cPágina 2\r\n\r\n".encode("utf-8")
    documento = await validar(original)
    assert documento.contenido == original
    assert documento.sha256 == hashlib.sha256(original).hexdigest()


# --- Formato ajeno compuesto solo por ASCII (PDF renombrado a .md) ----------

def construir_pdf_ascii(texto: str = "Memoria anual 2025") -> bytes:
    """PDF 1.4 mínimo, bien formado y compuesto solo por bytes ASCII (sin la línea
    de comentario binaria habitual). Los offsets de `xref` y `startxref` se calculan."""
    contenido = f"BT /F1 18 Tf 20 100 Td ({texto}) Tj ET"
    objetos = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(contenido)} >>\nstream\n{contenido}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    salida = "%PDF-1.4\n"
    offsets = []
    for numero, cuerpo in enumerate(objetos, start=1):
        offsets.append(len(salida))
        salida += f"{numero} 0 obj\n{cuerpo}\nendobj\n"
    inicio_xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n"
    salida += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    salida += (
        f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\n"
        f"startxref\n{inicio_xref}\n%%EOF\n"
    )
    return salida.encode("ascii")


PDF_ASCII = construir_pdf_ascii()


def test_el_pdf_de_prueba_esta_bien_formado_sin_depender_del_detector():
    # Verificación estructural independiente de `validacion`: el PDF no es un
    # texto con cabecera, es un PDF cuyas referencias internas son correctas.
    datos = PDF_ASCII
    assert all(byte < 128 for byte in datos)
    assert datos.startswith(b"%PDF-1.4\n") and datos.endswith(b"%%EOF\n")
    inicio_xref = int(datos.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert datos[inicio_xref:inicio_xref + 5] == b"xref\n"
    entradas = datos[inicio_xref:].split(b"trailer")[0].split(b"\n")[2:-1]
    assert len(entradas) == 6 and all(len(e) == 19 for e in entradas)  # 20 bytes con el salto
    for numero, entrada in enumerate(entradas[1:], start=1):
        offset = int(entrada[:10])
        assert datos[offset:].startswith(f"{numero} 0 obj\n".encode())
    assert b"/Size 6" in datos



def test_el_pdf_de_prueba_es_ascii_y_antes_superaba_todas_las_comprobaciones():
    texto = PDF_ASCII.decode("ascii")  # solo ASCII
    texto_interpretado, _ = validacion.decodificar_utf8(PDF_ASCII)
    validacion.validar_texto(texto_interpretado)  # sin controles prohibidos ni vacío
    assert validacion.diagnosticar_tablas(texto_interpretado).tablas == 0  # sin tablas
    assert texto.startswith("%PDF-") and texto.rstrip().endswith("%%EOF")


async def test_pdf_ascii_renombrado_a_md_se_rechaza_por_formato_no_por_utf8():
    upload = UploadFile(
        file=io.BytesIO(PDF_ASCII),
        filename="memoria.md",
        headers=Headers({"content-type": "text/markdown"}),
    )
    with pytest.raises(BusinessValidationError) as capturado:
        await validacion.validar_archivo(upload.filename, upload.read)
    assert capturado.value.code == "INVALID_FILE_CONTENT"
    assert capturado.value.details == {"formato_detectado": "PDF"}
    assert "PDF" in capturado.value.message and "Markdown" in capturado.value.message
    assert "Catalog" not in capturado.value.message  # sin contenido del archivo


@pytest.mark.parametrize(
    "datos",
    [
        pytest.param(PDF_ASCII.split(b"xref")[0], id="truncado-sin-EOF"),
        pytest.param(b"%PDF-1.7\n%%EOF\n", id="EOF-sin-objetos"),
        pytest.param(b"%PDF-2.0\n" + b"% relleno\n" * 20_000 + b"%%EOF\n", id="objetos-fuera-de-ventana"),
        pytest.param(b"%PDF-1.4\r\n7 0 obj\r\n<<>>\r\nendobj\r\n", id="crlf"),
    ],
)
async def test_pdf_incompleto_o_con_distinta_estructura_tambien_se_reconoce(datos):
    assert (await rechazo(datos)).details == {"formato_detectado": "PDF"}


@pytest.mark.parametrize(
    ("datos", "formato"),
    [
        (b"%!PS-Adobe-3.0\n%%Title: memoria\nshowpage\n", "PostScript"),
        (b"{\\rtf1\\ansi\\deff0 {\\fonttbl {\\f0 Times;}} Memoria\\par}", "RTF"),
    ],
)
async def test_otros_formatos_ascii_inequivocos_se_rechazan(datos, formato):
    error = await rechazo(datos)
    assert error.code == "INVALID_FILE_CONTENT"
    assert error.details == {"formato_detectado": formato}


@pytest.mark.parametrize(
    "texto",
    [
        # La cabecera sola, sin estructura de PDF, no basta.
        "%PDF-1.7\n\nUn Markdown que empieza con esa línea.\n",
        # Mención en una explicación.
        "# Formatos\n\nUn PDF empieza con `%PDF-1.7` y termina con `%%EOF`.\n",
        # Muestra completa dentro de un bloque de código, que no está en el offset 0.
        "# Cómo es un PDF\n\n```\n%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF\n```\n",
        # Empieza con texto y cita la cabecera al inicio de una línea.
        "Ejemplo:\n%PDF-1.4\n1 0 obj\nendobj\n%%EOF\n",
        # Firmas ajenas citadas fuera del offset 0.
        "# Notas\n\n%!PS-Adobe-3.0 y {\\rtf1 son firmas de otros formatos.\n",
    ],
)
async def test_markdown_que_menciona_firmas_de_otros_formatos_no_se_rechaza(texto):
    original = texto.encode("utf-8")
    documento = await validar(original)
    assert documento.contenido == original
    assert documento.sha256 == hashlib.sha256(original).hexdigest()


def test_la_deteccion_de_formato_no_modifica_los_bytes_ni_exige_firma_de_markdown():
    assert validacion.detectar_formato_incompatible(PDF_ASCII) == "PDF"
    assert validacion.detectar_formato_incompatible(b"") is None
    assert validacion.detectar_formato_incompatible(b"# Memoria\n") is None
    assert validacion.detectar_formato_incompatible("Sin firma alguna.\n".encode()) is None
    assert validacion.detectar_formato_incompatible(BOM + PDF_ASCII) is None  # solo offset 0


# --- Diagnóstico de tablas acotado en memoria ----------------------------------

def tabla_con_filas_malas(filas: int, malas: set[int], primera_linea: int):
    """(líneas, números de línea de las filas inválidas) de una tabla de 3 columnas."""
    lineas = ["| A | B | C |", "|---|---|---|"]
    invalidas = []
    for i in range(filas):
        malo = i in malas
        lineas.append("| 1 | 2 |" if malo else "| 1 | 2 | 3 |")
        if malo:
            invalidas.append(primera_linea + len(lineas) - 1)
    return lineas, invalidas


async def test_mas_de_20_inconsistencias_conserva_20_detalles_y_el_total_real():
    lineas, invalidas = tabla_con_filas_malas(60, set(range(60)), 1)
    texto = "\n".join(lineas)
    documento = await validar(texto.encode("utf-8"))  # no se rechaza
    diagnostico = documento.diagnostico_tablas
    assert diagnostico.total_inconsistencias == 60
    assert len(diagnostico.detalles) == validacion.MAX_ERRORES_TABLA_REPORTADOS == 20
    # Son las primeras 20, con su número de línea real y el contrato de cada detalle.
    assert [d.linea for d in diagnostico.detalles] == invalidas[:20] == list(range(3, 23))
    assert all((d.columnas_esperadas, d.columnas_encontradas) == (3, 2) for d in diagnostico.detalles)
    # La posición apunta exactamente al inicio de su fila en el texto.
    assert all(
        texto[d.inicio:].startswith("| 1 | 2 |") and texto[:d.inicio].count("\n") == d.linea - 1
        for d in diagnostico.detalles
    )
    # La advertencia expone el mismo acotamiento.
    assert len(documento.advertencias[0].detalles["inconsistencias"]) == 20


async def test_inconsistencias_entre_varias_tablas_se_cuentan_todas_con_maximo_global_de_20():
    lineas: list[str] = []
    invalidas: list[int] = []
    for _ in range(3):
        if lineas:
            lineas += ["", "Texto entre tablas.", ""]
        tabla, malas = tabla_con_filas_malas(15, set(range(12)), len(lineas) + 1)
        lineas += tabla
        invalidas += malas
    assert len(invalidas) == 36

    diagnostico = (await validar("\n".join(lineas).encode("utf-8"))).diagnostico_tablas
    assert (diagnostico.tablas, diagnostico.tablas_inconsistentes) == (3, 3)
    assert diagnostico.total_inconsistencias == 36
    # Orden de documento: la tabla 1 aporta 12 y la tabla 2, los 8 primeros.
    assert [d.linea for d in diagnostico.detalles] == invalidas[:20]
    assert invalidas[11] < invalidas[12] and invalidas[19] < invalidas[20]
    assert {d.linea_tabla for d in diagnostico.detalles} == {1, 21}


async def test_tablas_buenas_y_malas_cuentan_solo_las_inconsistentes():
    buena = "| A | B |\n|---|---|\n| 1 | 2 |"
    mala = "| A | B |\n|---|---|\n| 1 | 2 | 3 |"
    diagnostico = (await validar(f"{buena}\n\nTexto.\n\n{mala}\n".encode("utf-8"))).diagnostico_tablas
    assert (diagnostico.tablas, diagnostico.tablas_inconsistentes, diagnostico.total_inconsistencias) == (2, 1, 1)


async def test_el_diagnostico_sigue_contando_despues_de_alcanzar_el_limite():
    # Inconsistencias posteriores a las 20 primeras, en otra tabla, también cuentan.
    primera, _ = tabla_con_filas_malas(25, set(range(25)), 1)
    segunda, _ = tabla_con_filas_malas(1000, set(range(1000)), 1)
    texto = "\n".join([*primera, "", "fin", "", *segunda])
    diagnostico = (await validar(texto.encode("utf-8"))).diagnostico_tablas
    assert diagnostico.total_inconsistencias == 1025
    assert len(diagnostico.detalles) == 20


async def test_exactamente_20_y_21_inconsistencias():
    for errores, esperados in ((20, 20), (21, 20)):
        lineas, _ = tabla_con_filas_malas(errores, set(range(errores)), 1)
        diagnostico = (await validar("\n".join(lineas).encode("utf-8"))).diagnostico_tablas
        assert diagnostico.total_inconsistencias == errores
        assert len(diagnostico.detalles) == esperados


def test_durante_el_recorrido_de_varias_tablas_nunca_hay_mas_de_20_detalles_en_memoria(monkeypatch):
    maximos: list[int] = []
    registros: list[int] = []

    class Vigilado(validacion._ErroresDeTabla):
        def registrar(self, *args, **kwargs):
            super().registrar(*args, **kwargs)
            registros.append(self.total)
            maximos.append(len(self.detalles))  # tamaño tras CADA registro, no solo al final

    monkeypatch.setattr(validacion, "_ErroresDeTabla", Vigilado)
    tablas = []
    for _ in range(3):
        tabla, _ = tabla_con_filas_malas(50, set(range(50)), 1)
        tablas.append("\n".join(tabla))
    diagnostico = validacion.diagnosticar_tablas("\n\nTexto.\n\n".join(tablas))

    assert registros == list(range(1, 151))  # se contaron las 150, sin detenerse en 20
    assert max(maximos) == 20 and maximos[:20] == list(range(1, 21)) and set(maximos[20:]) == {20}
    assert diagnostico.total_inconsistencias == 150
    assert len(diagnostico.detalles) == 20


def test_el_acumulador_no_guarda_mas_de_20_detalles_aunque_registre_miles():
    errores = validacion._ErroresDeTabla()
    for linea in range(1, 5001):
        errores.registrar(linea, 3, 2)
        assert len(errores.detalles) <= validacion.MAX_ERRORES_TABLA_REPORTADOS
    assert errores.total == 5000
    assert [d[0] for d in errores.detalles] == list(range(1, 21))  # los primeros, no los últimos


async def test_las_tablas_inconsistentes_no_cambian_los_rechazos_de_admision():
    tabla_mala = "| A | B |\n|---|---|\n| 1 |\n"
    assert (await rechazo(b"")).code == "EMPTY_DOCUMENT"
    assert (await rechazo(b"\xff\xfe" + tabla_mala.encode())).code == "INVALID_ENCODING"
    assert (await rechazo((tabla_mala + "\x00").encode())).code == "INVALID_FILE_CONTENT"
    assert (await rechazo(PDF_ASCII)).code == "INVALID_FILE_CONTENT"
    assert (await rechazo(tabla_mala.encode() * 10, limite_bytes=20)).code == "FILE_TOO_LARGE"
    assert (await rechazo(tabla_mala.encode(), "memoria.pdf")).code == "INVALID_FILE_TYPE"


# --- T10 (parcial): SHA-256 sobre bytes originales ---------------------------

@pytest.mark.parametrize(
    ("datos", "esperado"),
    [
        (b"", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"),
        (b"abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"),
    ],
)
async def test_sha256_vectores_conocidos(datos, esperado):
    archivo = await validacion.leer_archivo_limitado(lector(datos), tamano_bloque=1)
    assert archivo.sha256 == esperado


async def test_mismo_contenido_con_otro_nombre_tiene_el_mismo_hash():
    datos = "# Memoria anual\nContenido idéntico.\n".encode("utf-8")
    a = await validar(datos, "memoria-2025.md")
    b = await validar(datos, "copia renombrada.MD")
    assert a.sha256 == b.sha256


async def test_un_byte_distinto_cambia_el_hash():
    a = await validar(b"# Memoria 2025\n")
    b = await validar(b"# Memoria 2024\n")
    assert a.sha256 != b.sha256
