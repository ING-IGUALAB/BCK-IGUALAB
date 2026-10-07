"""Ayudantes SOLO de pruebas para el coordinador de ingesta.

Los servicios externos (OCI, MinIO) se sustituyen por dobles; las GARANTÍAS que dependen de bases reales (reserva
concurrente, CHECK, bloqueo/cierre vectorial, publicación) se prueban contra PostgreSQL y pgvector AISLADOS
(`fabrica_pg`, `fabrica_vectorial` de `tests/services/conftest.py`). Un doble NO es evidencia de concurrencia
entre bases: donde se inyecta un fallo, se hace SOBRE una base real.
"""
import asyncio
import random
import uuid
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import text

from app.services.ingesta.coordinador import ConfigCoordinador, DependenciasIngesta
from app.services.ingesta.embeddings import IdentidadEmbeddings
from app.services.ingesta.fragmentacion import ParametrosFragmentacion
from tests.ayudantes_ingesta import AlmacenEnMemoria

DIM = 1536
IDENTIDAD = IdentidadEmbeddings("doble", "doble-1536", DIM)


def vector_de(texto: str) -> list[float]:
    azar = random.Random(zlib.crc32(texto.encode("utf-8")))
    return [azar.uniform(-1.0, 1.0) for _ in range(DIM)]


class ProveedorDoble:
    """Doble de `ProveedorEmbeddings`: vectores deterministas por texto. Registra las llamadas y permite inyectar
    un fallo en el lote N, una espera y una acción previa (p. ej. desactivar la empresa o simular una recuperación)."""

    def __init__(self, identidad: IdentidadEmbeddings = IDENTIDAD) -> None:
        self.identidad = identidad
        self.lotes: list[tuple[str, ...]] = []
        self.fallar_en_lote: int | None = None
        self.error: Exception = RuntimeError("detalle interno del proveedor con texto del documento")
        self.espera: float = 0.0
        self.antes_del_lote: dict[int, Callable[[], object]] = {}

    @property
    def llamadas(self) -> int:
        return len(self.lotes)

    async def generar_embeddings(self, textos: Sequence[str]) -> Sequence[Sequence[float]]:
        numero = len(self.lotes)
        self.lotes.append(tuple(textos))
        if numero in self.antes_del_lote:
            resultado = self.antes_del_lote[numero]()
            if asyncio.iscoroutine(resultado):
                await resultado
        if self.espera:
            await asyncio.sleep(self.espera)
        if self.fallar_en_lote == numero:
            raise self.error
        return [vector_de(t) for t in textos]


class _ResultadoVacio:
    rowcount = 0

    def all(self):
        return []

    def first(self):
        return None


class SesionVectorialConFallo:
    """Envuelve una sesión vectorial REAL e interviene en las sentencias SQL cuyo texto contiene `patron` (a partir
    de la `desde`-ésima coincidencia, `veces` como máximo): `accion="fallar"` lanza `error`; `"omitir"` NO la
    ejecuta y devuelve un resultado vacío (una limpieza que «no hace nada»); `"esperar"` espera `segundos` antes de
    ejecutarla. Todo lo demás se delega a la base real."""

    def __init__(self, real, patron: str, *, desde: int = 1, error: Exception | None = None, veces: int | None = None,
                 accion: str = "fallar", segundos: float = 0.0):
        self._real, self._patron, self._desde, self._veces = real, patron, desde, veces
        self._accion, self._segundos = accion, segundos
        self._error = error or RuntimeError("fallo inyectado en la base vectorial")
        self.coincidencias = 0
        self.intervenciones = 0

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)

    async def execute(self, sentencia, *args, **kwargs):
        if self._patron in str(sentencia):
            self.coincidencias += 1
            if self.coincidencias >= self._desde and (self._veces is None or self.intervenciones < self._veces):
                self.intervenciones += 1
                if self._accion == "fallar":
                    raise self._error
                if self._accion == "omitir":
                    return _ResultadoVacio()
                await asyncio.sleep(self._segundos)
        return await self._real.execute(sentencia, *args, **kwargs)


class FabricaVectorialInstrumentada:
    """Fábrica de sesiones vectoriales (como `async_sessionmaker`) que registra cuántas sesiones se abren y puede
    envolverlas con un fallo inyectado."""

    def __init__(self, base) -> None:
        self._base = base
        self.fallo: dict | None = None
        self.sesiones_abiertas = 0
        self.envolturas: list[SesionVectorialConFallo] = []

    def inyectar(self, patron: str, **kwargs) -> None:
        self.fallo = {"patron": patron, **kwargs}

    def quitar(self) -> None:
        self.fallo = None

    def __call__(self):
        fabrica = self

        class _Contexto:
            async def __aenter__(self_inner):
                self_inner._sesion = fabrica._base()
                real = await self_inner._sesion.__aenter__()
                fabrica.sesiones_abiertas += 1
                if fabrica.fallo is None:
                    return real
                self_inner._envoltura = SesionVectorialConFallo(real, **fabrica.fallo)
                fabrica.envolturas.append(self_inner._envoltura)
                return self_inner._envoltura

            async def __aexit__(self_inner, *exc):
                return await self_inner._sesion.__aexit__(*exc)

        return _Contexto()


class FabricaTransaccionalInstrumentada:
    """Fábrica transaccional que registra el NOMBRE de la tarea que abre cada sesión (el latido abre las suyas;
    nunca recibe la del ejecutor) y puede fallar de forma transitoria solo para el latido."""

    def __init__(self, base) -> None:
        self._base = base
        self.fallar_en_latido = False
        self.aperturas: list[str] = []

    def __call__(self):
        fabrica = self

        class _Contexto:
            async def __aenter__(self_inner):
                nombre = asyncio.current_task().get_name()
                fabrica.aperturas.append(nombre)
                if fabrica.fallar_en_latido and nombre == "latido-ingesta":
                    raise ConnectionError("base transaccional no disponible")
                self_inner._sesion = fabrica._base()
                return await self_inner._sesion.__aenter__()

            async def __aexit__(self_inner, *exc):
                return await self_inner._sesion.__aexit__(*exc)

        return _Contexto()

    @property
    def aperturas_del_latido(self) -> int:
        return sum(1 for n in self.aperturas if n == "latido-ingesta")


@dataclass
class Entorno:
    deps: DependenciasIngesta
    config: ConfigCoordinador
    almacen: AlmacenEnMemoria
    proveedor: ProveedorDoble
    fabrica_pg: object
    fabrica_vectorial: object
    vectorial: FabricaVectorialInstrumentada
    transaccional: FabricaTransaccionalInstrumentada
    usuario: object
    empresa: object
    metadatos: object
    extra: dict = field(default_factory=dict)

    async def sql(self, consulta: str, **parametros):
        async with self.fabrica_pg() as db:
            return (await db.execute(text(consulta), parametros)).all()

    async def sql_vectorial(self, consulta: str, **parametros):
        async with self.fabrica_vectorial() as db:
            return (await db.execute(text(consulta), parametros)).all()

    async def documento(self, documento_id: uuid.UUID | None = None):
        from app.models.documento_ingesta import Documento

        async with self.fabrica_pg() as db:
            if documento_id is None:
                filas = (await db.execute(text("SELECT id FROM documentos ORDER BY creado_en DESC LIMIT 1"))).all()
                documento_id = filas[0].id
            return await db.get(Documento, documento_id, populate_existing=True)

    async def documentos(self):
        from sqlalchemy import select

        from app.models.documento_ingesta import Documento

        async with self.fabrica_pg() as db:
            return list((await db.execute(select(Documento).order_by(Documento.creado_en))).scalars().all())

    async def conteo_vectorial(self, documento_id: uuid.UUID, ambiente: str = "development"):
        from app.services.ingesta import fragmentos_vectoriales as fv

        async with self.fabrica_vectorial() as v:
            return await fv.contar_fragmentos(v, ambiente=ambiente, documento_id=documento_id)

    async def auditoria(self):
        return await self.sql("SELECT tipo_evento, detalle, usuario_id, fecha_hora_utc FROM auditoria ORDER BY fecha_hora_utc")


TEXTO_CON_HALLAZGOS = (
    "# Informe de sostenibilidad 2025\n\n"
    "## Emisiones\n\n"
    "Reportamos el alcance 1 según GRI 305-1 y el alcance 2 según GRI 305-2, con datos verificados por terceros.\n\n"
    "## Cumplimiento legal\n\n"
    "En 2023 el OEFA impuso una multa de S/ 12,500 a la empresa, que fue pagada íntegramente.\n\n"
    "## Cierre\n\n"
    "Texto final del documento para asegurar varios fragmentos de tamaño pequeño en la prueba.\n"
)
TEXTO_OBSERVADO = (
    "# Memoria anual 2025\n\n"
    "## Presentación\n\n"
    "La empresa presenta su gestión del año con una narrativa general sin referencias a estándares.\n\n"
    "## Perspectivas\n\n"
    "Se continuará con las operaciones habituales durante el próximo periodo, sin hechos relevantes.\n"
)
TABLA_DETERIORADA = "\n| A | B | C |\n|---|---|\n| solo dos | celdas |\n| x | y | z | extra |\n"


def lote_sintetico(cantidad: int = 2, desde: int = 0, texto: str = "Texto sintético de prueba. " * 2):
    """Un `LoteEmbebido` con `cantidad` fragmentos consecutivos y vectores deterministas."""
    from app.services.ingesta.embeddings import FragmentoEmbebido, LoteEmbebido
    from app.services.ingesta.fragmentacion import Fragmento

    elementos, inicio = [], 0
    for i in range(cantidad):
        literal = f"{texto}{desde + i}"
        fragmento = Fragmento(
            indice=desde + i, inicio=inicio, fin=inicio + len(literal), texto_literal=literal,
            ruta_encabezados=(), contexto="", continuacion=False,
        )
        elementos.append(FragmentoEmbebido(fragmento, tuple(vector_de(literal))))
        inicio += len(literal)
    return LoteEmbebido(numero=0, identidad=IDENTIDAD, elementos=tuple(elementos))


def parametros_pequenos() -> ParametrosFragmentacion:
    return ParametrosFragmentacion(max_caracteres=140, max_caracteres_contexto=60)


def config_prueba(**cambios) -> ConfigCoordinador:
    base = dict(tamano_lote=2, fragmentacion=parametros_pequenos(), vigencia=timedelta(seconds=30))
    base.update(cambios)
    return ConfigCoordinador(**base)


def lector(datos: bytes):
    vista = memoryview(datos)
    posicion = 0

    async def leer(cantidad: int) -> bytes:
        nonlocal posicion
        bloque = bytes(vista[posicion:posicion + cantidad])
        posicion += len(bloque)
        return bloque

    return leer
