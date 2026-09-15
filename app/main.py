from fastapi import FastAPI
from app.routers import auth, usuarios

app = FastAPI(
    title="Igualab",
    version="0.1.0",
)

app.include_router(auth.router)
app.include_router(usuarios.router)


@app.get("/health", tags=["Infraestructura"])
async def health():
    """Endpoint testing confirme que el servicio está arriba antes de correr cualquier prueba funcional."""
    return {"status": "ok"}
