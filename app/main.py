from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import auth, usuarios

app = FastAPI(
    title="Igualab",
    version="0.1.0",
)


ORIGENES_PERMITIDOS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "https://igualab.vercel.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_PERMITIDOS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(usuarios.router)

# verificar si corre
@app.get("/health", tags=["Infraestructura"])
async def health():
    return {"status": "ok"}
