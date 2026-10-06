-- ============================================================
--  Base VECTORIAL (VECTOR_DATABASE_URL) — tabla fragmentos_documento
--  Motor : PostgreSQL 16 + pgvector (>= 0.5.0 para índices HNSW)
--  Modelo: cohere.embed-v4.0 (OCI Generative AI) — dimensión 1536
--
--  Aplicar sobre la base VECTORIAL, no la transaccional:
--    psql "$VECTOR_DATABASE_URL" -f db/vector/001_fragmentos_documento.sql
--  (VECTOR_DATABASE_URL usa el driver asyncpg; para psql, usar la URL con
--   el driver estándar: postgresql://usuario:password@host:puerto/base)
--
--  Nota: `documento_id` es una referencia LÓGICA (UUID) a `documentos(id)` de
--  la base transaccional. NO se declara FOREIGN KEY física porque son dos
--  bases distintas (ver Arquitectura: "sin claves foráneas físicas entre bases").
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS fragmentos_documento (
    id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    documento_id UUID          NOT NULL,
    indice       INTEGER       NOT NULL,
    seccion      VARCHAR(500),
    texto        TEXT          NOT NULL,
    embedding    VECTOR(1536)  NOT NULL,
    metadata     JSONB         NOT NULL DEFAULT '{}'::jsonb,
    creado_en    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT fragmentos_documento_documento_indice_uniq UNIQUE (documento_id, indice)
);

-- Índice HNSW obligatorio para búsqueda cosine (k=4):
--   ORDER BY embedding <=> :q LIMIT 4
CREATE INDEX IF NOT EXISTS idx_frag_hnsw
    ON fragmentos_documento
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMENT ON TABLE  fragmentos_documento            IS 'Fragmentos con embedding para RAG (cohere.embed-v4.0, 1536 dims).';
COMMENT ON COLUMN fragmentos_documento.documento_id IS 'Referencia lógica a documentos(id) (BD transaccional; sin FK física).';
COMMENT ON COLUMN fragmentos_documento.embedding    IS 'VECTOR(1536) — cohere.embed-v4.0 (OCI). No mezclar con embeddings de otro modelo.';
COMMENT ON COLUMN fragmentos_documento.metadata     IS 'empresa_id, año, tipo doc, fila de tabla origen, contexto de cita.';
