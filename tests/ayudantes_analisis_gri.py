"""Ayudantes SOLO de pruebas del Módulo 5 (análisis y validación GRI).

Los documentos se siembran en una base PostgreSQL AISLADA con un `analisis` generado por los detectores REALES de la ingesta
(`analizar_texto`, sin IA ni red): así las pruebas usan el mismo formato que persiste el coordinador. El esquema del módulo se
crea con la migración 0004 real (no con `create_all`), de modo que los disparadores de solo inserción existen de verdad.
"""
import contextlib
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
from fastapi import FastAPI

from app.database import get_db
from app.dependencies import get_current_user
from app.exception_handlers import register_exception_handlers
from app.migraciones.catalogo import ESQUEMA_TRANSACCIONAL
from app.models import Empresa, RolUsuario, SectorEmpresa, TipoDocumento, Usuario
from app.models.documento_ingesta import (
    Documento,
    EstadoProcesamiento,
    ResultadoAnalisis,
)
from app.request_id import RequestIDMiddleware
from app.routers import analisis_gri
from app.services.ingesta.analisis_ingesta import analizar_texto

TABLAS_MODULO_5 = (
    "analisis_gri_validaciones",
    "analisis_gri_historial",
    "analisis_gri_estados",
    "analisis_gri_control",
)
_ESQUEMA_REAL_APLICADO: set[str] = set()


async def asegurar_esquema_real(url_pg_aislado: str) -> None:
    """Reemplaza, una vez por clúster, las tablas del módulo creadas por `create_all` con las de la migración 0004."""
    if url_pg_aislado in _ESQUEMA_REAL_APLICADO:
        return
    conexion = await asyncpg.connect(url_pg_aislado.replace("+asyncpg", "", 1))
    try:
        await conexion.execute(
            "DROP TABLE IF EXISTS " + ", ".join(TABLAS_MODULO_5) + " CASCADE;"
            "DROP FUNCTION IF EXISTS igualab_rechazar_modificacion_analisis() CASCADE;"
        )
        await conexion.execute(ESQUEMA_TRANSACCIONAL.migraciones[3].sql())
    finally:
        await conexion.close()
    _ESQUEMA_REAL_APLICADO.add(url_pg_aislado)


async def crear_administrador(db, nombre: str = "Admin de prueba") -> Usuario:
    usuario = Usuario(
        nombre=nombre,
        correo=f"{uuid.uuid4().hex[:10]}@pruebas.invalid",
        password_hash="hash-de-prueba",
        rol=RolUsuario.ADMINISTRADOR,
        habilitado=True,
    )
    db.add(usuario)
    await db.commit()
    return usuario


async def crear_superadmin(db) -> Usuario:
    usuario = Usuario(
        nombre="Super de prueba",
        correo=f"{uuid.uuid4().hex[:10]}@pruebas.invalid",
        password_hash="hash-de-prueba",
        rol=RolUsuario.SUPERADMIN,
        habilitado=True,
    )
    db.add(usuario)
    await db.commit()
    return usuario


async def crear_empresa(db, nombre: str | None = None, activa: bool = True) -> Empresa:
    empresa = Empresa(nombre=nombre or f"Empresa {uuid.uuid4().hex[:8]}", sector=SectorEmpresa.MINERIA, activa=activa)
    db.add(empresa)
    await db.commit()
    return empresa


async def sembrar_documento(
    db,
    empresa: Empresa,
    usuario: Usuario,
    *,
    texto: str | None = None,
    analisis: dict | None = None,
    anio: int = 2025,
    tipo: TipoDocumento = TipoDocumento.MEMORIA_ANUAL,
    ambiente: str = "development",
    estado: EstadoProcesamiento = EstadoProcesamiento.COMPLETADO,
    publicado: bool = True,
    nombre: str | None = None,
) -> Documento:
    """Inserta un documento. COMPLETADO + publicado = elegible. `texto` se analiza con los detectores reales."""
    if analisis is None and texto is not None:
        analisis = analizar_texto(texto).a_dict()
    ahora = datetime.now(timezone.utc)
    identificador = uuid.uuid4()
    datos: dict = dict(
        id=identificador,
        ambiente=ambiente,
        empresa_id=empresa.id,
        anio=anio,
        tipo=tipo,
        sector=empresa.sector,
        nombre_archivo=nombre or f"{tipo.value.lower()}-{anio}.md",
        sha256=hashlib.sha256(identificador.bytes).hexdigest(),
        tamano_bytes=100,
        usuario_id=usuario.id,
        clave_original=f"{ambiente}/documentos/{identificador}/original.md",
        estado_procesamiento=estado,
        ejecucion_vigente_hasta=ahora + timedelta(hours=1),
        creado_en=ahora,
    )
    if estado is EstadoProcesamiento.COMPLETADO:
        assert analisis is not None, "un COMPLETADO necesita análisis"
        datos.update(
            resultado_analisis=ResultadoAnalisis(analisis["resultado"]),
            analisis=analisis,
            completado_en=ahora,
            original_almacenado_en=ahora,
            vector_escritura_intentada_en=ahora,
            vector_publicado_en=ahora if publicado else None,
            etapa_actual="FINALIZADO" if publicado else "PUBLICANDO",
        )
    elif estado is EstadoProcesamiento.FALLIDO:
        datos.update(motivo_fallo="INGESTION_ANALYSIS_FAILED", fallido_en=ahora, reserva_activa=False)
    documento = Documento(**datos)
    db.add(documento)
    await db.commit()
    return documento


async def publicar(db, documento: Documento) -> None:
    """Confirma la publicación vectorial de un COMPLETADO que la tenía pendiente."""
    from sqlalchemy import update

    await db.execute(
        update(Documento).where(Documento.id == documento.id).values(vector_publicado_en=datetime.now(timezone.utc))
    )
    await db.commit()


def construir_app(fabrica, usuario, *, con_ingesta: bool = False, analisis_error=None, ambiente: str | None = "development") -> FastAPI:
    """Aplicación mínima con los handlers y el middleware reales y SOLO el router del Módulo 5. `usuario=None` usa la
    autenticación real (sin sesión → 401). Sin gestor de ingesta: el módulo no lo necesita."""
    app = FastAPI()
    register_exception_handlers(app)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(analisis_gri.router)
    app.state.ingesta = None
    app.state.ingesta_error = ("INGESTION_NOT_CONFIGURED", "La ingesta de documentos no está disponible.", {"componentes": []})
    app.state.analisis_error = analisis_error

    async def db_de_prueba():
        async with fabrica() as sesion:
            yield sesion

    app.dependency_overrides[get_db] = db_de_prueba
    if usuario is not None:
        app.dependency_overrides[get_current_user] = lambda: usuario
    if ambiente is not None:
        app.dependency_overrides[analisis_gri.obtener_ambiente] = lambda: ambiente
    return app


@contextlib.asynccontextmanager
async def cliente_http(app: FastAPI):
    transporte = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transporte, base_url="http://prueba", timeout=60) as cliente:
        yield cliente


def ruta(empresa, anio: int = 2025, sufijo: str = "") -> str:
    return f"/analisis-gri/empresas/{empresa.id}/anios/{anio}{sufijo}"


def comprobar_error(respuesta, estado: int, codigo: str) -> dict:
    assert respuesta.status_code == estado, respuesta.text
    error = respuesta.json()["error"]
    assert error["code"] == codigo, respuesta.text
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["request_id"] != "unavailable"
    return error


def fila_de(analisis: dict, identidad: str) -> dict:
    return next(f for f in analisis["filas"] if f["identidad"] == identidad)


async def asignar(cliente, empresa, identidad: str, estado: str, version_fila: int = 0, anio: int = 2025):
    return await cliente.put(
        ruta(empresa, anio, f"/codigos/{identidad}/estado"),
        json={"estado": estado, "version_fila_esperada": version_fila},
    )


async def consultar(cliente, empresa, anio: int = 2025) -> dict:
    respuesta = await cliente.get(ruta(empresa, anio))
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.json()


async def validar(cliente, empresa, analisis: dict, anio: int = 2025):
    return await cliente.post(
        ruta(empresa, anio, "/validacion"),
        json={"version": analisis["version"], "huella_documentos": analisis["huella_documentos"]},
    )


# Markdown de prueba (los detectores reales los convierten en `analisis`).
MD_GRI_204_305 = """# Informe de sostenibilidad 2025

## Gestión de proveedores
En 2025 reportamos GRI 204-1: proporción de gasto en proveedores locales.

## Emisiones
Emisiones directas según GRI 305-1 (2016).
"""

MD_GRI_204_Y_302 = """# Memoria anual 2025

## Proveedores
Seguimos GRI 204 y GRI 204-1 de forma anual.

## Energía
Consumo energético en línea con GRI 302-1.
"""

MD_SOLO_SANCIONES = """# Sanciones
OSINERGMIN impuso una multa de S/ 120,000 a la empresa en 2024. El OEFA impuso una multa a la empresa en 2023.
"""

MD_OBSERVADO = "# Informe\nTexto sin códigos ni sanciones reconocibles.\n"
