-- ============================================================================================
--  Base VECTORIAL (VECTOR_DATABASE_URL) · tabla cierres_documento · Coordinador de ingesta
--  Requiere haber aplicado antes db/vector/001_fragmentos_documento.sql.
--
--  DDL PARA REVISIÓN. NO lo ejecuta la aplicación ni el arranque (D17). Se aplica igual que 001:
--    psql "<url con driver estándar postgresql://…>" -f db/vector/002_cierres_documento.sql
--  Es solo una tabla nueva: no modifica fragmentos_documento y se puede aplicar en caliente.
--
--  PARA QUÉ. Dos bases no comparten transacción. Un ejecutor lento, cancelado o ya recuperado puede
--  intentar insertar fragmentos DESPUÉS de que la compensación declaró limpio el documento. Al limpiar,
--  la compensación registra aquí un CIERRE del (ambiente, documento_id) en la MISMA transacción que
--  borra los fragmentos, y toda inserción o publicación posterior de ese documento se rechaza. Ambas
--  operaciones toman el mismo bloqueo asesor transaccional (pg_advisory_xact_lock sobre
--  ambiente:documento_id), de modo que o la inserción termina antes (y la limpieza la borra) o ve el
--  cierre y se rechaza: no hay ventana intermedia. Un documento cerrado no se reabre: cada reintento de
--  ingesta usa un documento_id nuevo.
-- ============================================================================================

CREATE TABLE cierres_documento (
    ambiente      VARCHAR(32)  NOT NULL,
    documento_id  UUID         NOT NULL,
    cerrado_en    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT pk_cierres_documento PRIMARY KEY (ambiente, documento_id),
    CONSTRAINT ck_cierres_ambiente CHECK (ambiente ~ '^[a-z0-9][a-z0-9_-]{0,31}$')
);

COMMENT ON TABLE  cierres_documento              IS 'Documentos cuya limpieza vectorial concluyó: no admiten nuevas inserciones ni publicación (impide escrituras tardías).';
COMMENT ON COLUMN cierres_documento.documento_id IS 'Referencia lógica a documentos(id) de la base transaccional (sin clave foránea entre bases).';
