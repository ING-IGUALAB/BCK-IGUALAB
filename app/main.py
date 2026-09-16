from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth, usuarios
from app.exception_handlers import register_exception_handlers
from app.logging_config import configure_logging
from app.request_id import RequestIDMiddleware
from scripts.crear_tablas import crear_tablas
from scripts.crear_superadmin_inicial import crear_superadmin_inicial

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await crear_tablas()
    await crear_superadmin_inicial()
    yield


app = FastAPI(
    title="Igualab",
    version="0.1.0",
    lifespan=lifespan,
)

register_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestIDMiddleware)

app.include_router(auth.router)
app.include_router(usuarios.router)

# verificar si corre
@app.get("/health", tags=["Infraestructura"])
async def health():
    return {"status": "ok"}
