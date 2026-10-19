"""Asistente IA (Módulo 6): recuperación LIMITADA a sector, empresa y año sobre PostgreSQL + pgvector REALES (RN-037).

REAL: la base transaccional aislada (empresa existente/activa/del sector, estado de cada documento), pgvector en un contenedor
desechable (el esquema de `app/migraciones/vectorial`, la vista `fragmentos_consultables`, la búsqueda por coseno y sus filtros) y
`responder_consulta` de punta a punta. CONTROLADOS: los proveedores de embeddings y de chat (sin red, sin OCI ni DeepSeek).

Todos los fragmentos llevan el MISMO vector que la consulta: una búsqueda global los devolvería todos, de modo que lo que no aparece
lo excluye un filtro y no la distancia. Si Docker o la imagen local de pgvector no están disponibles estas pruebas se OMITEN.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app.core.exceptions import BusinessValidationError
from app.models import Empresa, SectorEmpresa, TipoDocumento
from app.models.documento_ingesta import EstadoProcesamiento
from app.modules.asistente import service as servicio
from app.modules.asistente.domain.citas import MOTIVO_SIN_FRAGMENTOS
from tests.ayudantes_analisis_gri import MD_GRI_204_305, crear_administrador, crear_empresa, sembrar_documento
from tests.services.test_asistente import afirmacion, respuesta
from tests.services.test_asistente_postgres import LlmControlado
from tests.services.test_fragmentos_vectoriales import IDENTIDAD, eje, guardar, lote, publicar, sintetico

AMBIENTE = "development"
VECTOR = eje(0)


class EmbeddingsControlado:
    identidad = IDENTIDAD

    def __init__(self):
        self.llamadas = 0

    async def generar_embeddings_consulta(self, textos):
        self.llamadas += 1
        return [list(VECTOR) for _ in textos]


def texto_de(clave: str) -> str:
    return f"Fragmento {clave}: la empresa redujo el consumo de agua en 12 % durante el periodo."


async def indexar(fabrica_vectorial, documento, empresa, *, indice=0, sector=None, clave=None, ambiente=AMBIENTE):
    """Guarda un fragmento de `documento` en pgvector con el mismo vector que la consulta y lo publica."""
    fragmento = sintetico(indice, texto_de(clave or str(documento.id)[:8]))
    await guardar(
        fabrica_vectorial, lote([fragmento], [VECTOR]), ambiente=ambiente, documento_id=documento.id, empresa_id=empresa.id,
        anio=documento.anio, tipo=documento.tipo, sector=sector or empresa.sector,
    )
    return await publicar(fabrica_vectorial, ambiente=ambiente, documento=documento.id)


@pytest_asyncio.fixture
async def mundo(fabrica_pg, fabrica_vectorial):
    async with fabrica_pg() as db:
        admin = await crear_administrador(db, "Ana Admin")
        a = await crear_empresa(db, "Minera Andina")  # MINERIA
        b = await crear_empresa(db, "Minera Rival")  # MINERIA
        c = Empresa(nombre="Energía Sur", sector=SectorEmpresa.ENERGIA, activa=True)
        db.add(c)
        await db.commit()
        docs = SimpleNamespace(
            a25=await sembrar_documento(db, a, admin, texto=MD_GRI_204_305, anio=2025, nombre="andina-2025.md"),
            a24=await sembrar_documento(db, a, admin, texto=MD_GRI_204_305, anio=2024, nombre="andina-2024.md"),
            b25=await sembrar_documento(db, b, admin, texto=MD_GRI_204_305, anio=2025, nombre="rival-2025.md"),
            c25=await sembrar_documento(db, c, admin, texto=MD_GRI_204_305, anio=2025, nombre="sur-2025.md"),
            a25_fallido=await sembrar_documento(
                db, a, admin, estado=EstadoProcesamiento.FALLIDO, tipo=TipoDocumento.REPORTE_SOSTENIBILIDAD_GRI, anio=2025,
                nombre="andina-fallido.md",
            ),
        )
    for nombre, empresa in (("a25", a), ("a24", a), ("b25", b), ("c25", c), ("a25_fallido", a)):
        assert await indexar(fabrica_vectorial, getattr(docs, nombre), empresa, clave=nombre) == 1
    return SimpleNamespace(
        fabrica_pg=fabrica_pg, fabrica_vectorial=fabrica_vectorial, admin=admin, a=a, b=b, c=c, docs=docs,
        embeddings=EmbeddingsControlado(), llm=LlmControlado(),
    )


async def consultar(mundo, *, empresa=None, sector=SectorEmpresa.MINERIA, anio=2025):
    async with mundo.fabrica_pg() as db, mundo.fabrica_vectorial() as db_vectorial:
        return await servicio.responder_consulta(
            db, db_vectorial, pregunta="¿Cómo evolucionó el consumo de agua?", ambiente=AMBIENTE,
            proveedor_embeddings=mundo.embeddings, proveedor_llm=mundo.llm,
            sector=sector, empresa_id=(empresa or mundo.a).id, anio=anio,
        )


async def test_la_busqueda_real_solo_entrega_fragmentos_de_la_empresa_el_anio_y_el_sector_pedidos(mundo):
    mundo.llm.contenido = respuesta(afirmacion("La empresa redujo su consumo de agua.", (1,), "redujo el consumo de agua en 12 %"))

    resultado = await consultar(mundo)

    # Al modelo solo llegó el fragmento de Minera Andina 2025: ni otro año, ni otra empresa (aunque sea del mismo sector), ni otro
    # sector, ni un documento que la base transaccional marca como FALLIDO.
    contexto = mundo.llm.usuario
    assert texto_de("a25") in contexto
    for ajeno in ("a24", "b25", "c25", "a25_fallido"):
        assert texto_de(ajeno) not in contexto
    assert contexto.count("<<<FRAGMENTO ") == 1  # una sola apertura: un único fragmento (el cierre es «<<<FIN FRAGMENTO»)
    assert [(c.documento_nombre, c.empresa_nombre, c.anio) for c in resultado.citas] == [("andina-2025.md", "Minera Andina", 2025)]
    assert resultado.citas[0].documento_id == mundo.docs.a25.id
    assert resultado.sin_evidencia is False


@pytest.mark.parametrize("anio,empresa,sector,esperado", [
    (2024, "a", SectorEmpresa.MINERIA, "a24"),
    (2025, "b", SectorEmpresa.MINERIA, "b25"),
    (2025, "c", SectorEmpresa.ENERGIA, "c25"),
])
async def test_cada_seleccion_recupera_unicamente_su_propio_documento(mundo, anio, empresa, sector, esperado):
    mundo.llm.contenido = respuesta(afirmacion("La empresa redujo su consumo de agua.", (1,), "redujo el consumo de agua en 12 %"))

    resultado = await consultar(mundo, empresa=getattr(mundo, empresa), sector=sector, anio=anio)

    assert getattr(mundo.docs, esperado).id == resultado.citas[0].documento_id
    assert len(resultado.citas) == 1
    assert mundo.llm.usuario.count("<<<FRAGMENTO ") == 1


async def test_un_fragmento_con_otro_sector_en_pgvector_no_se_recupera_aunque_sea_de_la_misma_empresa_y_anio(mundo):
    """El filtro por sector se aplica en SQL: el segundo fragmento es de Minera Andina 2025 (empresa y año correctos) pero está
    indexado con sector ENERGIA, y sin ese filtro entraría al contexto."""
    await indexar(mundo.fabrica_vectorial, mundo.docs.a25, mundo.a, indice=1, sector=SectorEmpresa.ENERGIA, clave="a25-otro-sector")
    mundo.llm.contenido = respuesta(afirmacion("La empresa redujo su consumo de agua.", (1,), "redujo el consumo de agua en 12 %"))

    await consultar(mundo)

    assert texto_de("a25") in mundo.llm.usuario
    assert texto_de("a25-otro-sector") not in mundo.llm.usuario


async def test_un_fragmento_de_otro_ambiente_no_se_recupera(mundo):
    otro = SimpleNamespace(id=mundo.docs.a25.id, anio=2025, tipo=mundo.docs.a25.tipo)
    await indexar(mundo.fabrica_vectorial, otro, mundo.a, indice=1, clave="a25-qa", ambiente="qa")
    mundo.llm.contenido = respuesta(afirmacion("La empresa redujo su consumo de agua.", (1,), "redujo el consumo de agua en 12 %"))

    await consultar(mundo)

    assert texto_de("a25-qa") not in mundo.llm.usuario


async def test_sin_fragmentos_publicados_para_la_seleccion_no_se_llama_al_modelo(mundo):
    async with mundo.fabrica_pg() as db:
        vacia = await crear_empresa(db, "Minera Sin Documentos")

    resultado = await consultar(mundo, empresa=vacia)

    assert resultado.sin_evidencia is True
    assert resultado.motivo == MOTIVO_SIN_FRAGMENTOS
    assert mundo.embeddings.llamadas == 1  # se embebió la pregunta, pero no hubo contexto
    assert mundo.llm.llamadas == 0
    assert resultado.citas == []


async def test_una_referencia_a_otra_empresa_no_existe_en_el_contexto_y_descarta_la_afirmacion(mundo):
    """El modelo (o un fragmento malicioso) pide citar [2], que correspondería a otra empresa: ese fragmento nunca estuvo."""
    mundo.llm.contenido = respuesta(afirmacion("La empresa rival redujo su consumo.", (2,), "redujo el consumo de agua en 12 %"))

    resultado = await consultar(mundo)

    assert resultado.sin_evidencia is True
    assert resultado.citas == []
    assert resultado.afirmaciones == []


async def test_la_seleccion_se_valida_en_la_base_antes_de_buscar(mundo):
    for empresa, sector, anio in ((mundo.a, SectorEmpresa.ENERGIA, 2025), (mundo.c, SectorEmpresa.MINERIA, 2025)):
        with pytest.raises(BusinessValidationError) as capturado:
            await consultar(mundo, empresa=empresa, sector=sector, anio=anio)
        assert capturado.value.code == "COMPANY_SECTOR_MISMATCH"
    async with mundo.fabrica_pg() as db:
        deshabilitada = await crear_empresa(db, "Minera Cerrada", activa=False)
    with pytest.raises(BusinessValidationError) as capturado:
        await consultar(mundo, empresa=deshabilitada)
    assert capturado.value.code == "COMPANY_INACTIVE"
    assert mundo.embeddings.llamadas == 0  # ni embedding ni búsqueda ni modelo
    assert mundo.llm.llamadas == 0


async def test_una_empresa_inexistente_responde_no_encontrada_sin_buscar(mundo):
    from app.core.exceptions import NotFoundError

    fantasma = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(NotFoundError):
        await consultar(mundo, empresa=fantasma)
    assert mundo.embeddings.llamadas == 0
