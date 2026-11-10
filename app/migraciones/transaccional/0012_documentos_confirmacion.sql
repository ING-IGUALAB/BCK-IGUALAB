-- ============================================================================================
--  Base TRANSACCIONAL (DATABASE_URL) · tabla documentos · Doble check de ingesta (CONFIRMANDO)
--  Migración 0012 (transaccional): requiere la 0002 (columnas del coordinador).
--
--  Migración automática: la aplica `app/migraciones` al arrancar, UNA sola vez, dentro de una transacción, y la registra en
--  `igualab_migraciones`. NO se ejecuta a mano ni se repite en cada arranque. Las 0001–0011 NO se tocan.
--
--  QUÉ AÑADE
--    1. Columna NULABLE `confirmacion_pendiente_en`: instante en que la ingesta entró en la etapa
--       `CONFIRMANDO` (tras el análisis GRI/sanciones) y esperó la decisión del SuperAdmin
--       (continuar o cancelar). Sin DEFAULT: ninguna fila existente queda en pausa y esta migración
--       NO ejecuta ningún UPDATE. Solo informativa: la pausa se reconoce por `etapa_actual`.
--    2. Amplía `ck_documentos_etapa_valida` (mismo nombre): admite el nuevo valor `CONFIRMANDO`
--       además de las siete etapas anteriores.
--
--  QUÉ NO CAMBIA
--    * `estado_procesamiento` sigue siendo un enum de tres valores (EN_PROCESO durante la pausa).
--    * Los índices únicos parciales de reserva no se tocan.
--    * El original (MinIO) y los fragmentos (base vectorial) ya escritos se conservan mientras
--      la pausa siga activa; solo se eliminan si el SuperAdmin cancela (compensación).
--
--  Datos existentes: toda fila actual cumple las restricciones nuevas (la columna es NULL y
--  `CONFIRMANDO` solo se amplía el conjunto de etapas), así que ADD CONSTRAINT no puede fallar.
--  Sin IF NOT EXISTS: si algo ya existe, falla y deshace todo.
--
--  Prueba: tests/services/test_documento_modelo.py (CHECK en SQLite) y
--  tests/services/test_documento_coordinador_postgres.py (equivalencia de DDL en PostgreSQL).
-- ============================================================================================

ALTER TABLE documentos
    ADD COLUMN confirmacion_pendiente_en TIMESTAMP WITH TIME ZONE;

ALTER TABLE documentos DROP CONSTRAINT ck_documentos_etapa_valida;

ALTER TABLE documentos
    ADD CONSTRAINT ck_documentos_etapa_valida CHECK (
        etapa_actual IN (
            'RESERVADO',
            'ALMACENANDO_ORIGINAL',
            'INDEXANDO',
            'ANALIZANDO',
            'CONFIRMANDO',
            'COMPLETANDO',
            'PUBLICANDO',
            'FINALIZADO'
        )
    );
