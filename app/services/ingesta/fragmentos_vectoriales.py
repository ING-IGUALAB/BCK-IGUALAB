"""Persistencia de fragmentos y embeddings en PostgreSQL + pgvector (Etapa 4B).

Esquema: `db/vector/001_fragmentos_documento.sql` (NO lo ejecuta la aplicación). Cada función
recibe una `AsyncSession` de la base VECTORIAL (`app.database_vectorial`), la usa de forma
secuencial y exige que llegue SIN transacción abierta: cada operación es una única transacción
corta que termina con commit o rollback.

CONTRATO DE VISIBILIDAD (léase antes de usar este módulo desde un coordinador o la recuperación)
- `guardar_lote` inserta SIEMPRE con `publicado = false`. Un lote se persiste en UNA transacción: si
  algo falla, no queda ninguna fila de ese lote (rollback). Las filas de lotes anteriores del mismo
  documento ya confirmados NO se deshacen: si falla un lote posterior, el coordinador debe llamar a
  `eliminar_fragmentos`.
- `publicar_fragmentos` y `eliminar_fragmentos` actúan sobre TODOS los fragmentos de un documento
  dentro de UN ambiente, nunca más, y son idempotentes (repetirlas es seguro).
- `buscar_similares` lee SOLO la vista `fragmentos_consultables` (los no publicados no existen para
  ella). Búsqueda EXACTA por coseno: sin índice vectorial todavía.
- **`publicado` NO sustituye el estado COMPLETADO de la base transaccional.** Dos bases no comparten
  transacción: no existe una secuencia atómica entre ellas. Para que un fragmento sea recuperable
  deben cumplirse AMBAS condiciones:
    (a) en la base transaccional, el documento está `COMPLETADO` con `resultado_analisis` no nulo y su
        empresa activa (`documento_service.documentos_recuperables`), y
    (b) en la base vectorial, el fragmento está `publicado`.
  Orden obligatorio para el coordinador: insertar fragmentos (no publicados) → publicar el documento
  en la base transaccional (`publicar_documento`) → `publicar_fragmentos`. Si falla entre los dos
  últimos pasos el documento queda COMPLETADO con fragmentos invisibles (dirección segura; un
  reconciliador repite `publicar_fragmentos`, idempotente). NUNCA publicar fragmentos antes de
  completar el documento. Quien recupere debe comprobar (a) sobre los `documento_id` devueltos
  (`buscar_similares` no puede consultar la otra base) y descartar los demás.
- **COMPENSACIÓN CONJUNTA** (coordinador): `documento_service.compensar_documento` limpia MinIO Y esta base
  (`cerrar_y_eliminar_fragmentos` + `contar_fragmentos == 0`) antes de liberar la reserva. El CIERRE del
  documento (`cierres_documento`, tomado en la misma transacción que el borrado y serializado con la inserción
  mediante un bloqueo asesor) impide que una escritura tardía de un ejecutor anterior reintroduzca fragmentos
  después de declarada la limpieza; `publicar_fragmentos` tampoco publica un documento cerrado.
- Limpieza de un documento fallido: eliminar su original (MinIO), `eliminar_fragmentos` y comprobar
  con `contar_fragmentos` que no quedan filas antes de liberar la reserva. No hace falta una columna
  nueva en `documentos`: todo documento con fragmentos tuvo antes el intento de almacenamiento
  registrado (paso 3 de la Etapa 4A, el coordinador debe indexar solo después), por lo que su
  compensación ya queda PENDIENTE; y `eliminar_fragmentos` es idempotente. Una inserción de resultado
  incierto que aterrice después de la limpieza dejaría filas NO publicadas e invisibles (invariante
  «recuperable ⇔ publicado ∧ COMPLETADO»); un barrido de huérfanos queda pendiente.

AISLAMIENTO POR IDENTIDAD DEL EMBEDDING: la dimensión 1536 no prueba compatibilidad (dos modelos
distintos pueden producir 1536 componentes en espacios vectoriales incomparables). `buscar_similares`
recibe EXPLÍCITAMENTE la `IdentidadEmbeddings` del vector de consulta, valida su dimensión y filtra por
proveedor y modelo EN SQL, antes de `ORDER BY` y `LIMIT`: un fragmento de otro modelo nunca compite ni
desplaza resultados, aunque su vector sea más parecido.

VALIDACIÓN (antes de tocar la base): el ambiente; UUID de documento y empresa; año, tipo y sector; la
identidad del modelo con dimensión 1536; que cada vector tenga exactamente 1536 componentes numéricas
(no booleanos) y que, en la representación FLOAT32 que almacena pgvector, todas sean finitas, alguna sea
distinta de cero y la norma al cuadrado sea representable en float32 (entre FLT_MIN y FLT_MAX). Verificado
contra pgvector real: `[1e-50]*1536` se convierte en el vector cero, y `[1e-30]*1536` o `[1e19]*1536`
(float32 válidos y no nulos) producen un coseno NaN porque pgvector acumula en float32; todos se
rechazan antes de persistir o buscar. Además, la consistencia de cada `Fragmento`
(`0 <= inicio < fin`, `len(texto_literal) == fin - inicio`, textos sin NUL). El texto literal y el contexto se guardan sin modificar; el texto enviado al proveedor sigue
siendo `contexto + texto_literal` (`Fragmento.texto_embedding`), que esta capa no calcula ni altera.

El vector viaja como literal de texto convertido con `CAST(... AS vector)`: no hace falta registrar un
códec en asyncpg ni añadir el paquete `pgvector` de Python.
"""
import math
import struct
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from numbers import Real

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import ConflictError
from app.services.ingesta.almacenamiento import validar_ambiente
from app.services.ingesta.embeddings import IdentidadEmbeddings, LoteEmbebido
from app.services.ingesta.fragmentacion import SEPARADOR_RUTA

DIMENSION_VECTORIAL = 1536
MAXIMO_RESULTADOS = 1000
_FLT_MAX = 3.4028234663852886e38  # mayor float32 finito
_FLT_MIN = 1.1754943508222875e-38  # menor float32 normal
_UNICA = "uq_fragmentos_ambiente_documento_indice"

_INSERTAR = text(
    "INSERT INTO fragmentos_documento (ambiente, documento_id, indice, empresa_id, anio, tipo, sector, "
    "texto_literal, contexto, inicio, fin, continuacion, ruta_encabezados, embedding, "
    "embedding_proveedor, embedding_modelo, embedding_dimension) VALUES ("
    ":ambiente, :documento_id, :indice, :empresa_id, :anio, :tipo, :sector, "
    ":texto_literal, :contexto, :inicio, :fin, :continuacion, CAST(:ruta_encabezados AS text[]), "
    "CAST(CAST(:embedding AS text) AS vector), :proveedor, :modelo, :dimension)"
)
_PUBLICAR = text(
    "UPDATE fragmentos_documento SET publicado = true, publicado_en = now() "
    "WHERE ambiente = :ambiente AND documento_id = :documento_id AND NOT publicado "
    "AND NOT EXISTS (SELECT 1 FROM cierres_documento c "
    "WHERE c.ambiente = fragmentos_documento.ambiente AND c.documento_id = fragmentos_documento.documento_id)"
)
# Cierre del documento (`db/vector/002_cierres_documento.sql`): el bloqueo asesor serializa la inserción
# de fragmentos con la limpieza, de modo que ninguna escritura tardía aterriza después de la limpieza.
_BLOQUEAR = text("SELECT pg_advisory_xact_lock(hashtextextended(:clave, 0))")
_CERRADO = text("SELECT 1 FROM cierres_documento WHERE ambiente = :ambiente AND documento_id = :documento_id")
_CERRAR = text(
    "INSERT INTO cierres_documento (ambiente, documento_id) VALUES (:ambiente, :documento_id) "
    "ON CONFLICT (ambiente, documento_id) DO NOTHING"
)
_ELIMINAR = text("DELETE FROM fragmentos_documento WHERE ambiente = :ambiente AND documento_id = :documento_id")
_CONTAR = text(
    "SELECT count(*) AS total, count(*) FILTER (WHERE publicado) AS publicados FROM fragmentos_documento "
    "WHERE ambiente = :ambiente AND documento_id = :documento_id"
)


@dataclass(frozen=True)
class ConteoFragmentos:
    total: int
    publicados: int


@dataclass(frozen=True)
class FragmentoRecuperado:
    """Un fragmento PUBLICADO leído de la vista. No implica que su documento esté COMPLETADO."""

    documento_id: uuid.UUID
    indice: int
    texto_literal: str
    contexto: str
    inicio: int
    fin: int
    continuacion: bool
    ruta_encabezados: tuple[str, ...]
    empresa_id: uuid.UUID
    anio: int
    tipo: str
    sector: str
    embedding_proveedor: str
    embedding_modelo: str
    similitud: float  # coseno: 1 - distancia

    @property
    def seccion(self) -> str:
        return SEPARADOR_RUTA.join(self.ruta_encabezados)

    @property
    def texto_embedding(self) -> str:
        return self.contexto + self.texto_literal


# --- Validación ---------------------------------------------------------------------------

def _exigir_sin_transaccion(db: AsyncSession) -> None:
    if db.in_transaction():
        raise RuntimeError("La sesión vectorial debe llegar sin transacción abierta.")


def _a_float32(componente: float) -> float:
    """El valor que almacenará pgvector (float4); `OverflowError` si no cabe en float32."""
    return struct.unpack("f", struct.pack("f", componente))[0]


def _validar_vector(vector: object, nombre: str) -> str:
    """Devuelve el literal de texto del vector YA convertido a float32, o lanza ValueError sin repetir
    sus valores. Lo que se valida es exactamente lo que se almacena o se compara en pgvector."""
    if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
        raise ValueError(f"{nombre} debe ser una secuencia de números.")
    if len(vector) != DIMENSION_VECTORIAL:
        raise ValueError(f"{nombre} debe tener exactamente {DIMENSION_VECTORIAL} componentes; tiene {len(vector)}.")
    convertido: list[float] = []
    for componente in vector:
        if isinstance(componente, bool) or not isinstance(componente, Real) or not math.isfinite(componente):
            raise ValueError(f"{nombre} contiene una componente que no es un número finito.")
        try:
            valor = _a_float32(float(componente))
        except OverflowError:
            raise ValueError(f"{nombre} contiene una componente fuera del rango de float32.") from None
        if not math.isfinite(valor):
            raise ValueError(f"{nombre} contiene una componente no finita en float32.")
        convertido.append(valor)
    if not any(valor != 0.0 for valor in convertido):
        raise ValueError(f"{nombre} es el vector cero en float32 (p. ej. por componentes demasiado pequeñas).")
    norma_cuadrada = math.fsum(valor * valor for valor in convertido)
    if not _FLT_MIN <= norma_cuadrada <= _FLT_MAX:
        raise ValueError(
            f"{nombre} tiene una norma no representable en float32: el coseno de pgvector no estaría definido."
        )
    return "[" + ",".join(repr(valor) for valor in convertido) + "]"


def _texto(valor: object) -> str:
    return valor.value if isinstance(valor, Enum) else valor  # type: ignore[return-value]


def _exigir_texto_no_vacio(nombre: str, valor: object) -> str:
    valor = _texto(valor)
    if not isinstance(valor, str) or not valor.strip() or "\x00" in valor:
        raise ValueError(f"{nombre} debe ser un texto no vacío.")
    return valor


def _exigir_uuid(nombre: str, valor: object) -> uuid.UUID:
    if not isinstance(valor, uuid.UUID):
        raise ValueError(f"{nombre} debe ser un UUID.")
    return valor


def _filas(
    ambiente: str, documento_id: uuid.UUID, empresa_id: uuid.UUID, anio: int, tipo: object, sector: object,
    lote: LoteEmbebido,
) -> list[dict]:
    ambiente = validar_ambiente(ambiente)
    _exigir_uuid("documento_id", documento_id)
    _exigir_uuid("empresa_id", empresa_id)
    if isinstance(anio, bool) or not isinstance(anio, int) or anio < 2000:
        raise ValueError("El año debe ser un entero de al menos 2000.")
    tipo, sector = _exigir_texto_no_vacio("tipo", tipo), _exigir_texto_no_vacio("sector", sector)
    if not isinstance(lote, LoteEmbebido):
        raise ValueError("lote debe ser un LoteEmbebido.")
    identidad = lote.identidad
    if identidad.dimension != DIMENSION_VECTORIAL:
        raise ValueError(
            f"La dimensión del modelo ({identidad.dimension}) no coincide con la de la base vectorial ({DIMENSION_VECTORIAL})."
        )
    if not lote.elementos:
        raise ValueError("El lote no contiene fragmentos.")
    filas, vistos = [], set()
    for elemento in lote.elementos:
        f = elemento.fragmento
        if isinstance(f.indice, bool) or not isinstance(f.indice, int) or f.indice < 0 or f.indice in vistos:
            raise ValueError("Los índices de fragmento deben ser enteros no negativos y únicos dentro del lote.")
        vistos.add(f.indice)
        if (
            isinstance(f.inicio, bool) or isinstance(f.fin, bool)
            or not isinstance(f.inicio, int) or not isinstance(f.fin, int)
            or not 0 <= f.inicio < f.fin
        ):
            raise ValueError("Las posiciones del fragmento deben cumplir 0 <= inicio < fin.")
        if not isinstance(f.texto_literal, str) or not isinstance(f.contexto, str) or len(f.texto_literal) != f.fin - f.inicio:
            raise ValueError("El texto literal debe medir exactamente fin - inicio caracteres.")
        if "\x00" in f.texto_literal or "\x00" in f.contexto:
            raise ValueError("El texto del fragmento contiene caracteres NUL.")
        if not isinstance(f.continuacion, bool):
            raise ValueError("`continuacion` debe ser booleano.")
        ruta = tuple(f.ruta_encabezados)
        if not all(isinstance(t, str) and "\x00" not in t for t in ruta):
            raise ValueError("La ruta de encabezados debe contener solo textos sin NUL.")
        filas.append(
            dict(
                ambiente=ambiente, documento_id=documento_id, indice=f.indice, empresa_id=empresa_id, anio=anio,
                tipo=tipo, sector=sector, texto_literal=f.texto_literal, contexto=f.contexto, inicio=f.inicio,
                fin=f.fin, continuacion=f.continuacion, ruta_encabezados=list(ruta),
                embedding=_validar_vector(elemento.vector, "El vector"),
                proveedor=identidad.proveedor, modelo=identidad.modelo, dimension=identidad.dimension,
            )
        )
    return filas


# --- Operaciones -----------------------------------------------------------------------------

async def guardar_lote(
    db: AsyncSession,
    *,
    ambiente: str,
    documento_id: uuid.UUID,
    empresa_id: uuid.UUID,
    anio: int,
    tipo: object,
    sector: object,
    lote: LoteEmbebido,
) -> int:
    """Inserta un lote como NO publicado, en una sola transacción. Devuelve las filas insertadas.

    Valida todo antes de tocar la base. Si falla la inserción (también por un índice repetido) se hace
    rollback y no queda ninguna fila del lote. Un índice ya persistido para ese documento y ambiente
    produce `ConflictError("FRAGMENTS_ALREADY_PERSISTED")`; cualquier otra `IntegrityError` se propaga.
    """
    filas = _filas(ambiente, documento_id, empresa_id, anio, tipo, sector, lote)
    _exigir_sin_transaccion(db)
    try:
        # Mismo bloqueo que la limpieza: o este lote termina antes (y la limpieza lo borra) o ve el cierre.
        await db.execute(_BLOQUEAR, {"clave": _clave_de_bloqueo(ambiente, documento_id)})
        if (await db.execute(_CERRADO, {"ambiente": ambiente, "documento_id": documento_id})).first() is not None:
            await db.rollback()
            raise ConflictError(
                "FRAGMENTS_DOCUMENT_CLOSED",
                "El documento ya fue cerrado por su limpieza: no admite nuevos fragmentos.",
            )
        await db.execute(_INSERTAR, filas)
        await db.commit()
    except ConflictError:
        raise
    except IntegrityError as exc:
        await db.rollback()
        if _UNICA in f"{exc.orig} {getattr(getattr(exc.orig, '__cause__', None), 'constraint_name', '')}":
            raise ConflictError(
                "FRAGMENTS_ALREADY_PERSISTED",
                "Ya existen fragmentos con esos índices para el documento en este ambiente.",
            ) from None
        raise
    except Exception:
        await db.rollback()
        raise
    return len(filas)


def _clave_de_bloqueo(ambiente: str, documento_id: uuid.UUID) -> str:
    return f"fragmentos:{ambiente}:{documento_id}"


async def _por_documento(db: AsyncSession, sentencia, ambiente: str, documento_id: uuid.UUID):
    ambiente = validar_ambiente(ambiente)
    _exigir_uuid("documento_id", documento_id)
    _exigir_sin_transaccion(db)
    try:
        resultado = await db.execute(sentencia, {"ambiente": ambiente, "documento_id": documento_id})
        filas = resultado.rowcount if sentencia is not _CONTAR else resultado.one()
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return filas


async def publicar_fragmentos(db: AsyncSession, *, ambiente: str, documento_id: uuid.UUID) -> int:
    """Publica TODOS los fragmentos aún no publicados del documento en ese ambiente. Idempotente:
    devuelve cuántos se publicaron ahora (0 si ya estaban publicados o no existen). Solo debe llamarse
    DESPUÉS de completar el documento en la base transaccional (ver el contrato del módulo)."""
    return await _por_documento(db, _PUBLICAR, ambiente, documento_id)


async def eliminar_fragmentos(db: AsyncSession, *, ambiente: str, documento_id: uuid.UUID) -> int:
    """Elimina TODOS los fragmentos (publicados o no) del documento en ese ambiente y de ningún otro
    documento ni ambiente. Idempotente: devuelve cuántas filas eliminó (0 si no había)."""
    return await _por_documento(db, _ELIMINAR, ambiente, documento_id)


async def cerrar_y_eliminar_fragmentos(db: AsyncSession, *, ambiente: str, documento_id: uuid.UUID) -> int:
    """Limpieza DEFINITIVA de un documento fallido: en UNA transacción toma el bloqueo del documento,
    registra su CIERRE y elimina todos sus fragmentos. Devuelve cuántas filas eliminó.

    Tras el commit, ninguna inserción (`guardar_lote` → `FRAGMENTS_DOCUMENT_CLOSED`) ni publicación puede
    afectar a ese documento: una escritura tardía de un ejecutor anterior ya no puede reintroducir contenido.
    Idempotente. Requiere `db/vector/002_cierres_documento.sql`."""
    ambiente = validar_ambiente(ambiente)
    _exigir_uuid("documento_id", documento_id)
    _exigir_sin_transaccion(db)
    parametros = {"ambiente": ambiente, "documento_id": documento_id}
    try:
        await db.execute(_BLOQUEAR, {"clave": _clave_de_bloqueo(ambiente, documento_id)})
        await db.execute(_CERRAR, parametros)
        eliminadas = (await db.execute(_ELIMINAR, parametros)).rowcount
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return eliminadas


async def contar_fragmentos(db: AsyncSession, *, ambiente: str, documento_id: uuid.UUID) -> ConteoFragmentos:
    """Total y publicados de un documento: sirve para comprobar la limpieza (total == 0) o la publicación."""
    fila = await _por_documento(db, _CONTAR, ambiente, documento_id)
    return ConteoFragmentos(total=fila.total, publicados=fila.publicados)


async def buscar_similares(
    db: AsyncSession,
    *,
    ambiente: str,
    vector_consulta: Sequence[float],
    identidad: IdentidadEmbeddings,
    empresa_ids: Sequence[uuid.UUID],
    limite: int,
    anio: int | None = None,
    tipo: object | None = None,
    sector: object | None = None,
) -> list[FragmentoRecuperado]:
    """Búsqueda EXACTA por coseno entre los fragmentos PUBLICADOS (vista `fragmentos_consultables`),
    de más a menos similar, con desempate determinista por documento e índice.

    `identidad` es la del embedding de CONSULTA y es obligatoria: se valida su dimensión y se filtra por
    proveedor y modelo en SQL antes de ordenar y limitar (no se deduce compatibilidad por tener 1536
    componentes). Los fragmentos de otro modelo quedan excluidos aunque sean más parecidos.

    `empresa_ids` es obligatorio: la base vectorial no sabe qué empresas están activas, así que quien
    recupera pasa las vigentes (obtenidas de la base transaccional). Una lista vacía devuelve `[]`.
    Los resultados NO están comprobados contra el estado COMPLETADO: esta búsqueda todavía necesita la
    comprobación transaccional de `documento_service.documentos_recuperables` sobre sus `documento_id`
    ANTES de entregar o usar cualquier resultado.
    """
    ambiente = validar_ambiente(ambiente)
    if not isinstance(identidad, IdentidadEmbeddings):
        raise ValueError("identidad debe ser la IdentidadEmbeddings del embedding de consulta.")
    if identidad.dimension != DIMENSION_VECTORIAL:
        raise ValueError(
            f"La dimensión del modelo de consulta ({identidad.dimension}) no coincide con la de la base vectorial ({DIMENSION_VECTORIAL})."
        )
    literal = _validar_vector(vector_consulta, "El vector de consulta")
    if isinstance(limite, bool) or not isinstance(limite, int) or not 1 <= limite <= MAXIMO_RESULTADOS:
        raise ValueError(f"limite debe ser un entero entre 1 y {MAXIMO_RESULTADOS}.")
    if isinstance(empresa_ids, (str, bytes)) or empresa_ids is None:
        raise ValueError("empresa_ids debe ser una secuencia de UUID.")
    empresas = [_exigir_uuid("empresa_ids", e) for e in empresa_ids]
    if not empresas:
        return []
    condiciones = [
        "ambiente = :ambiente",
        "embedding_proveedor = :proveedor",
        "embedding_modelo = :modelo",
        "empresa_id = ANY(CAST(:empresas AS uuid[]))",
    ]
    parametros: dict = {
        "ambiente": ambiente, "proveedor": identidad.proveedor, "modelo": identidad.modelo,
        "empresas": empresas, "q": literal, "limite": limite,
    }
    if anio is not None:
        if isinstance(anio, bool) or not isinstance(anio, int):
            raise ValueError("anio debe ser un entero.")
        condiciones.append("anio = :anio")
        parametros["anio"] = anio
    if tipo is not None:
        condiciones.append("tipo = :tipo")
        parametros["tipo"] = _exigir_texto_no_vacio("tipo", tipo)
    if sector is not None:
        condiciones.append("sector = :sector")
        parametros["sector"] = _exigir_texto_no_vacio("sector", sector)
    consulta = text(
        "SELECT documento_id, indice, texto_literal, contexto, inicio, fin, continuacion, ruta_encabezados, "
        "empresa_id, anio, tipo, sector, embedding_proveedor, embedding_modelo, "
        "1 - (embedding <=> CAST(CAST(:q AS text) AS vector)) AS similitud "
        "FROM fragmentos_consultables WHERE " + " AND ".join(condiciones) +
        " ORDER BY embedding <=> CAST(CAST(:q AS text) AS vector), documento_id, indice LIMIT :limite"
    )
    _exigir_sin_transaccion(db)
    try:
        filas = (await db.execute(consulta, parametros)).all()
        await db.commit()  # lectura: se cierra la transacción
    except Exception:
        await db.rollback()
        raise
    return [
        FragmentoRecuperado(
            documento_id=f.documento_id, indice=f.indice, texto_literal=f.texto_literal, contexto=f.contexto,
            inicio=f.inicio, fin=f.fin, continuacion=f.continuacion, ruta_encabezados=tuple(f.ruta_encabezados),
            empresa_id=f.empresa_id, anio=f.anio, tipo=f.tipo, sector=f.sector,
            embedding_proveedor=f.embedding_proveedor, embedding_modelo=f.embedding_modelo,
            similitud=float(f.similitud),
        )
        for f in filas
    ]
