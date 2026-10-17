from contextlib import asynccontextmanager
import logging
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.modules.auth import router as auth
from app.modules.usuarios import router as usuarios
from app.modules.empresas import router as empresas
from app.modules.ingesta import router as documentos
from app.modules.analisis_gri import router as analisis_gri
from app.modules.asistente import router as asistente
from app.modules.reportes import router as reportes
from app.modules.auditoria import router as auditoria
from app.core.exception_handlers import ManejadorExcepcionesMiddleware, register_exception_handlers
from app.core.logging_config import configure_logging, paso_de_arranque
from app.core.request_id import RequestIDMiddleware
from app.core.config import settings
from scripts.crear_tablas import crear_tablas
from scripts.crear_superadmin_inicial import crear_superadmin_inicial
from app.modules.analisis_gri.application.arranque import preparar_esquema_analisis
from app.modules.ingesta.application.gestor import detener_ingesta, iniciar_ingesta

configure_logging()
logger = logging.getLogger("igualab.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    inicio = time.monotonic()
    logger.info("Arranque | lifespan | inicio (ambiente=%s)", settings.APP_ENV)
    with paso_de_arranque(logger, "1/6 Creación de tablas existentes"):
        await crear_tablas()
    with paso_de_arranque(logger, "2/6 Inicialización del SuperAdmin"):
        await crear_superadmin_inicial()
    # Esquema del Módulo 5 (análisis GRI): solo la base transaccional, sin depender de la ingesta. Si falla, ese módulo responde 503.
    await preparar_esquema_analisis(app)
    # Recursos de ingesta (pasos 3 a 6, registrados dentro): una vez, y cierre ordenado. Si falta configuración o falla la
    # preparación del esquema la aplicación arranca igual.
    await iniciar_ingesta(app)
    logger.info("Arranque | lifespan | fin en %.2f s (ingesta %s)", time.monotonic() - inicio,
                "habilitada" if getattr(app.state, "ingesta", None) is not None else "NO disponible")
    try:
        yield
    finally:
        await detener_ingesta(app)


app = FastAPI(
    title="Igualab",
    version="0.1.0",
    lifespan=lifespan,
)

register_exception_handlers(app)
app.add_middleware(RequestIDMiddleware)
# Dentro de CORS (se registra antes que CORSMiddleware, que queda outermost): así un 500
# inesperado conserva las cabeceras Access-Control-Allow-* y el navegador puede leerlo.
app.add_middleware(ManejadorExcepcionesMiddleware)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(usuarios.router)
app.include_router(empresas.router)
app.include_router(documentos.router)
app.include_router(analisis_gri.router)
app.include_router(asistente.router)
app.include_router(reportes.router)
app.include_router(auditoria.router)

# verificar si corre
@app.get("/health", tags=["Infraestructura"])
async def health():
    return {"status": "ok"}
