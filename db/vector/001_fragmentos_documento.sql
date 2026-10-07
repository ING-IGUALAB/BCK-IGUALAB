-- ============================================================================================
--  Base VECTORIAL (VECTOR_DATABASE_URL) · tabla fragmentos_documento · Etapa 4B
--  PostgreSQL 16 + pgvector (probado con pgvector 0.8.6 en una instancia desechable).
--  Modelo de embeddings: cohere.embed-v4.0 (OCI Generative AI), 1536 componentes.
--
--  DDL PARA REVISIÓN. NO lo ejecuta la aplicación ni el arranque (no hay mecanismo de
--  migraciones aprobado: D17). Quien administre la base vectorial de cada ambiente lo aplica:
--    psql "<url con driver estándar postgresql://…>" -f db/vector/001_fragmentos_documento.sql
--  (VECTOR_DATABASE_URL usa el driver asyncpg; para psql use la URL sin «+asyncpg».)
--
--  PRERREQUISITO (superusuario o rol con privilegio; no se ejecuta aquí a propósito):
--    CREATE EXTENSION IF NOT EXISTS vector;
--
--  Sin `IF NOT EXISTS` en la tabla: si ya existe con otra definición, que falle en lugar de
--  aceptarla en silencio.
--
--  REGLAS DE ESTE ESQUEMA
--  * `documento_id`, `empresa_id` son referencias LÓGICAS a la base transaccional: NO hay claves
--    foráneas entre bases distintas.
--  * `ambiente` (development | qa | uat) separa los espacios; la unicidad incluye el ambiente.
--  * Se conservan por separado la cita (`texto_literal`) y el `contexto` con que se calculó el
--    embedding. El texto enviado al proveedor es `contexto || texto_literal`: esta tabla no lo
--    cambia ni lo guarda duplicado.
--  * `inicio`/`fin`: posiciones de caracteres (puntos de código) del texto interpretado, inicio
--    inclusivo y fin exclusivo, tal como las produce el fragmentador; `texto_literal` mide
--    exactamente `fin - inicio` caracteres.
--  * Los fragmentos se insertan NO publicados. `publicado` NO sustituye la comprobación de que el
--    documento esté COMPLETADO (con análisis ejecutado y empresa activa) en la base transaccional:
--    ver `docs/ingesta/03-arquitectura-y-decisiones.md` (Etapa 4B). Toda lectura para recuperación
--    usa la vista `fragmentos_consultables`, que excluye lo no publicado.
--  * Sin índice vectorial (HNSW/IVFFlat) todavía: primero búsqueda EXACTA por coseno; los índices
--    se decidirán con datos y filtros reales. La clave única cubre (ambiente, documento_id).
-- ============================================================================================

CREATE TABLE fragmentos_documento (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    ambiente             VARCHAR(32)  NOT NULL,
    documento_id         UUID         NOT NULL,
    indice               INTEGER      NOT NULL,

    -- Filtros de recuperación, copiados del documento por el servidor al insertar.
    empresa_id           UUID         NOT NULL,
    anio                 INTEGER      NOT NULL,
    tipo                 VARCHAR(64)  NOT NULL,
    sector               VARCHAR(64)  NOT NULL,

    -- Metadatos del fragmentador (sin límites arbitrarios de longitud).
    texto_literal        TEXT         NOT NULL,
    contexto             TEXT         NOT NULL DEFAULT '',
    inicio               INTEGER      NOT NULL,
    fin                  INTEGER      NOT NULL,
    continuacion         BOOLEAN      NOT NULL,
    ruta_encabezados     TEXT[]       NOT NULL DEFAULT '{}',

    embedding            VECTOR(1536) NOT NULL,
    embedding_proveedor  TEXT         NOT NULL,
    embedding_modelo     TEXT         NOT NULL,
    embedding_dimension  SMALLINT     NOT NULL,

    publicado            BOOLEAN      NOT NULL DEFAULT false,
    publicado_en         TIMESTAMPTZ,
    creado_en            TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_fragmentos_ambiente_documento_indice UNIQUE (ambiente, documento_id, indice),
    CONSTRAINT ck_fragmentos_ambiente CHECK (ambiente ~ '^[a-z0-9][a-z0-9_-]{0,31}$'),
    CONSTRAINT ck_fragmentos_indice_no_negativo CHECK (indice >= 0),
    CONSTRAINT ck_fragmentos_anio_minimo CHECK (anio >= 2000),
    CONSTRAINT ck_fragmentos_tipo_no_vacio CHECK (length(trim(tipo)) > 0),
    CONSTRAINT ck_fragmentos_sector_no_vacio CHECK (length(trim(sector)) > 0),
    CONSTRAINT ck_fragmentos_posiciones CHECK (inicio >= 0 AND fin > inicio),
    CONSTRAINT ck_fragmentos_literal_coincide_con_posiciones CHECK (length(texto_literal) = fin - inicio),
    CONSTRAINT ck_fragmentos_dimension_1536 CHECK (embedding_dimension = 1536),
    CONSTRAINT ck_fragmentos_vector_1536 CHECK (vector_dims(embedding) = embedding_dimension),
    CONSTRAINT ck_fragmentos_modelo_no_vacio CHECK (length(trim(embedding_proveedor)) > 0 AND length(trim(embedding_modelo)) > 0),
    CONSTRAINT ck_fragmentos_publicado_con_fecha CHECK (publicado = (publicado_en IS NOT NULL))
);

COMMENT ON TABLE  fragmentos_documento                  IS 'Fragmentos con embedding para RAG (Etapa 4B). Se insertan no publicados; publicado NO sustituye COMPLETADO en la base transaccional.';
COMMENT ON COLUMN fragmentos_documento.documento_id     IS 'Referencia lógica a documentos(id) de la base transaccional (sin clave foránea entre bases).';
COMMENT ON COLUMN fragmentos_documento.texto_literal    IS 'Cita real: texto[inicio:fin] del texto interpretado, sin normalizar.';
COMMENT ON COLUMN fragmentos_documento.contexto         IS 'Contexto añadido al embedding; el texto enviado al proveedor es contexto || texto_literal.';
COMMENT ON COLUMN fragmentos_documento.inicio           IS 'Posición de carácter (punto de código) inicial, inclusiva.';
COMMENT ON COLUMN fragmentos_documento.fin              IS 'Posición de carácter (punto de código) final, exclusiva.';
COMMENT ON COLUMN fragmentos_documento.embedding        IS 'VECTOR(1536). No mezclar con embeddings de otro modelo (ver embedding_modelo).';

-- Única vía de lectura para recuperación: excluye los fragmentos no publicados.
CREATE VIEW fragmentos_consultables AS
SELECT id, ambiente, documento_id, indice, empresa_id, anio, tipo, sector,
       texto_literal, contexto, inicio, fin, continuacion, ruta_encabezados,
       embedding, embedding_proveedor, embedding_modelo, embedding_dimension,
       publicado_en, creado_en
FROM fragmentos_documento
WHERE publicado;

COMMENT ON VIEW fragmentos_consultables IS 'Fragmentos publicados. Usarla para toda lectura de recuperación; además hay que comprobar COMPLETADO en la base transaccional.';
