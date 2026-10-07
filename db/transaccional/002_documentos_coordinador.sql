-- ============================================================================================
--  Base TRANSACCIONAL (DATABASE_URL) · tabla documentos · ACTUALIZACIÓN para el coordinador de ingesta
--  PostgreSQL. Para una tabla `documentos` YA creada con el DDL de la Etapa 4A.
--  (En una instalación nueva use db/transaccional/001_documentos.sql: ya incluye todo esto; no
--  aplique ambos scripts sobre la misma base.)
--
--  DDL PARA REVISIÓN. NO lo ejecuta la aplicación ni el arranque, y importar los modelos tampoco crea
--  nada (el modelo Documento no está registrado en app.models: D17). Quien administre cada base lo aplica:
--    psql "<url con driver estándar postgresql://…>" -f db/transaccional/002_documentos_coordinador.sql
--
--  Sin IF NOT EXISTS: si una columna o restricción ya existe, el script falla y deshace todo (una sola
--  transacción) en lugar de aceptarla en silencio. ADD COLUMN con DEFAULT constante no reescribe la tabla
--  (PostgreSQL >= 11); las restricciones se validan contra las filas existentes, que no tienen datos
--  nuevos y por tanto las cumplen. Un documento COMPLETADO anterior al coordinador (no debería haber
--  ninguno: nada publicaba en la Etapa 4A) quedaría con la publicación vectorial pendiente.
--
--  Nota: las claves del JSON se comprueban con coalesce(...): un CHECK cuyo resultado es NULL (clave ausente) se
--  CUMPLE en PostgreSQL, así que sin coalesce una clave ausente pasaría.
--
--  Prueba: tests/services/test_documento_coordinador_postgres.py aplica el DDL de la Etapa 4A y luego este
--  script en un PostgreSQL aislado y comprueba que el esquema resultante es idéntico al de instalación nueva.
-- ============================================================================================

BEGIN;

ALTER TABLE documentos
    -- Resultado completo de analizar_documento (a_dict()); resultado_analisis es solo la clasificación.
    ADD COLUMN analisis JSONB,
    -- Progreso persistente por operación (el identificador de operación es documentos.id).
    ADD COLUMN etapa_actual VARCHAR(32) NOT NULL DEFAULT 'RESERVADO',
    ADD COLUMN fragmentos_procesados INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN fragmentos_total INTEGER,
    ADD COLUMN progreso_actualizado_en TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    ADD COLUMN advertencias JSONB NOT NULL DEFAULT '[]',
    -- Rastro duradero de la escritura y la publicación en la base vectorial.
    ADD COLUMN vector_escritura_intentada_en TIMESTAMP WITH TIME ZONE,
    ADD COLUMN vector_publicado_en TIMESTAMP WITH TIME ZONE,
    ADD COLUMN vector_publicacion_intentos INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN vector_ultimo_error VARCHAR(64);

ALTER TABLE documentos
    ADD CONSTRAINT ck_documentos_etapa_valida CHECK (etapa_actual IN ('RESERVADO', 'ALMACENANDO_ORIGINAL', 'INDEXANDO', 'ANALIZANDO', 'COMPLETANDO', 'PUBLICANDO', 'FINALIZADO')),
    ADD CONSTRAINT ck_documentos_fragmentos_progreso CHECK (fragmentos_procesados >= 0 AND (fragmentos_total IS NULL OR (fragmentos_total >= 0 AND fragmentos_procesados <= fragmentos_total))),
    ADD CONSTRAINT ck_documentos_publicacion_intentos CHECK (vector_publicacion_intentos >= 0),
    ADD CONSTRAINT ck_documentos_vector_publicado_solo_completado CHECK (vector_publicado_en IS NULL OR estado_procesamiento = 'COMPLETADO'),
    ADD CONSTRAINT ck_documentos_vector_publicado_con_intento CHECK (vector_publicado_en IS NULL OR vector_escritura_intentada_en IS NOT NULL),
    ADD CONSTRAINT ck_documentos_sin_analisis_si_fallido CHECK (analisis IS NULL OR estado_procesamiento <> 'FALLIDO'),
    ADD CONSTRAINT ck_documentos_analisis_consistente CHECK (resultado_analisis IS NULL OR analisis IS NULL OR analisis->>'resultado' = resultado_analisis::text),
    ADD CONSTRAINT ck_documentos_analisis_estructura CHECK (analisis IS NULL OR (jsonb_typeof(analisis) = 'object' AND coalesce(analisis->>'resultado', '') IN ('CON_HALLAZGOS', 'OBSERVADO') AND length(coalesce(analisis->>'version_catalogo', '')) > 0 AND coalesce(jsonb_typeof(analisis->'motivos'), '') = 'array' AND coalesce(jsonb_typeof(analisis->'gri'), '') = 'array' AND coalesce(jsonb_typeof(analisis->'sanciones'), '') = 'array' AND coalesce(jsonb_typeof(analisis->'advertencias'), '') = 'array')),
    ADD CONSTRAINT ck_documentos_advertencias_arreglo CHECK (jsonb_typeof(advertencias) = 'array');

COMMIT;
