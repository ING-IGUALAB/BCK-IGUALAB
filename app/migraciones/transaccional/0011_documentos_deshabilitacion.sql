-- ============================================================================================
--  Base TRANSACCIONAL (DATABASE_URL) · tabla documentos · Deshabilitación de documentos (ACT-02)
--  Migración 0011 (transaccional): requiere la 0002 (columnas del coordinador) y la tabla `usuarios`.
--
--  Migración automática: la aplica `app/migraciones` al arrancar, UNA sola vez, dentro de una transacción, y la registra en
--  `igualab_migraciones`. NO se ejecuta a mano ni se repite en cada arranque. La 0001 y la 0002 NO se tocan.
--
--  QUÉ AÑADE
--    1. Tres columnas NULABLES que registran el retiro administrativo de un documento ya COMPLETADO y PUBLICADO:
--       `deshabilitado_en`, `deshabilitado_por` (FK a `usuarios`) y `motivo_deshabilitacion`. Sin DEFAULT: ninguna fila existente
--       queda deshabilitada y esta migración NO ejecuta ningún UPDATE (no deshabilita datos). `estado_procesamiento` sigue siendo
--       COMPLETADO: la deshabilitación se representa aparte.
--    2. Amplía `ck_documentos_reserva_solo_liberada_si_limpio` (mismo nombre): además del caso original, un FALLIDO sin limpieza
--       pendiente (NINGUNA o COMPLETADA, intacto), ahora admite liberar la reserva de un COMPLETADO PUBLICADO que consta como
--       deshabilitado. Un COMPLETADO sin deshabilitar, un EN_PROCESO y un FALLIDO con limpieza PENDIENTE siguen sin poder liberarla.
--    3. Cuatro CHECK nuevos: las tres columnas van juntas; el motivo no es vacío; solo se deshabilita un COMPLETADO publicado; y un
--       documento deshabilitado NO conserva la reserva (con la regla anterior: deshabilitado <=> COMPLETADO sin reserva).
--
--  QUÉ NO CAMBIA
--    * Los índices únicos parciales `uq_documentos_sha256_activo` y `uq_documentos_empresa_anio_tipo_activo` (ambos
--      `WHERE reserva_activa`) se conservan tal cual. Efecto de liberar la reserva: el cupo empresa/año/tipo y el SHA-256 del documento
--      deshabilitado dejan de estar ocupados, de modo que volver a subir los MISMOS bytes es posible mientras ninguna otra reserva
--      activa del ambiente tenga ese SHA-256 o ese cupo; si la hay, el índice sigue rechazando la carga.
--    * El original (MinIO), los fragmentos (base vectorial) y las referencias históricas (reportes, validaciones) no se tocan.
--
--  Datos existentes: toda fila actual cumple las restricciones nuevas (las columnas son NULL y la regla de reserva solo se amplía),
--  así que las validaciones de ADD CONSTRAINT no pueden fallar por datos. Sin IF NOT EXISTS: si algo ya existe, falla y deshace todo.
--
--  Prueba: tests/services/test_documento_deshabilitacion_postgres.py (migración sobre datos existentes en PostgreSQL aislado).
-- ============================================================================================

ALTER TABLE documentos
    ADD COLUMN deshabilitado_en TIMESTAMP WITH TIME ZONE,
    ADD COLUMN deshabilitado_por UUID,
    ADD COLUMN motivo_deshabilitacion VARCHAR(500);

ALTER TABLE documentos
    ADD CONSTRAINT documentos_deshabilitado_por_fkey FOREIGN KEY (deshabilitado_por) REFERENCES usuarios (id);

ALTER TABLE documentos DROP CONSTRAINT ck_documentos_reserva_solo_liberada_si_limpio;

ALTER TABLE documentos
    ADD CONSTRAINT ck_documentos_reserva_solo_liberada_si_limpio CHECK (
        reserva_activa
        OR (estado_procesamiento = 'FALLIDO' AND estado_compensacion IN ('NINGUNA', 'COMPLETADA'))
        OR (estado_procesamiento = 'COMPLETADO' AND vector_publicado_en IS NOT NULL AND deshabilitado_en IS NOT NULL)
    ),
    ADD CONSTRAINT ck_documentos_deshabilitacion_completa CHECK (
        (deshabilitado_en IS NULL) = (deshabilitado_por IS NULL) AND (deshabilitado_en IS NULL) = (motivo_deshabilitacion IS NULL)
    ),
    ADD CONSTRAINT ck_documentos_motivo_deshabilitacion_no_vacio CHECK (
        motivo_deshabilitacion IS NULL OR length(trim(motivo_deshabilitacion)) > 0
    ),
    ADD CONSTRAINT ck_documentos_deshabilitado_solo_completado_publicado CHECK (
        deshabilitado_en IS NULL OR (estado_procesamiento = 'COMPLETADO' AND vector_publicado_en IS NOT NULL)
    ),
    ADD CONSTRAINT ck_documentos_deshabilitado_sin_reserva CHECK (deshabilitado_en IS NULL OR NOT reserva_activa);
