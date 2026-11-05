-- Migración 0006 (transaccional): reportes de prospección VERSIONADOS (BUG-15; LTX:RN-041 reformulada, RN-040 sin cambios).
-- La aplica `app/migraciones` al arrancar, una sola vez y en una transacción; no se ejecuta a mano.
--
-- QUÉ CAMBIA. Hasta ahora una empresa/año tenía UN reporte (UNIQUE empresa_id, anio). Ahora admite varias versiones inmutables:
--   * `version`        ordinal de la emisión dentro de la empresa/año (>= 1). Los reportes existentes son la versión 1.
--   * `validacion_id`  validación GRI (`analisis_gri_validaciones.id`) que respalda el reporte. Una validación respalda a lo sumo
--                      UN reporte. NULL SOLO en los reportes anteriores a esta migración (ver «COMPATIBILIDAD»).
--   * UNIQUE (empresa_id, anio, version) sustituye a UNIQUE (empresa_id, anio).
--
-- COMPATIBILIDAD CON LOS REPORTES EXISTENTES. Se conservan sin tocar ninguna fila: mismo id, PDF, sha256, snapshot, fecha y autor.
-- `version` pasa a 1 por el valor por defecto de la columna nueva (PostgreSQL >= 11 lo resuelve sin reescribir ni actualizar filas,
-- por eso NO se dispara el disparador de solo inserción). `validacion_id` queda NULL: la tabla es de solo inserción, no se
-- desactiva el disparador ni se deduce un vínculo a partir del snapshot; el servicio lo trata como «origen histórico» y, solo para
-- decidir si ya refleja la última validación, compara el número y la huella de documentos que el propio snapshot guardó.
--
-- POR QUÉ ES IDEMPOTENTE Y NO DECLARA OBJETOS. `reportes_prospeccion` la crea `create_all` ANTES de las migraciones: en una instalación
-- nueva ya tiene la forma del modelo actual (sin la clave foránea, porque `analisis_gri_validaciones` no la crea `create_all`); en una
-- base desplegada tiene la forma anterior. Igual que la 0005, cada sentencia admite que el objeto ya exista, y como las tablas ya
-- existen no hay adopción por firma posible.
--
-- No usa TRUNCATE, CASCADE ni desactiva disparadores. Requiere PostgreSQL >= 12 y las migraciones 0004 y 0005.

-- 1. Columnas. El DEFAULT 1 solo sirve para materializar la versión de las filas existentes; se retira para que toda inserción
-- nueva indique su versión (igual que en una instalación nueva, donde el modelo no define valor por defecto).
ALTER TABLE reportes_prospeccion ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE reportes_prospeccion ALTER COLUMN version DROP DEFAULT;
ALTER TABLE reportes_prospeccion ADD COLUMN IF NOT EXISTS validacion_id UUID;

DO $$
BEGIN
	-- 2. Restricciones nuevas. Primero se crean y después se retira la anterior: nunca queda la tabla sin protección.
	IF NOT EXISTS (
		SELECT 1 FROM pg_constraint
		WHERE conname = 'ck_reporte_version_positiva' AND conrelid = 'public.reportes_prospeccion'::regclass
	) THEN
		ALTER TABLE reportes_prospeccion ADD CONSTRAINT ck_reporte_version_positiva CHECK (version >= 1);
	END IF;

	IF NOT EXISTS (
		SELECT 1 FROM pg_constraint
		WHERE conname = 'uq_reporte_empresa_anio_version' AND conrelid = 'public.reportes_prospeccion'::regclass
	) THEN
		ALTER TABLE reportes_prospeccion ADD CONSTRAINT uq_reporte_empresa_anio_version UNIQUE (empresa_id, anio, version);
	END IF;

	-- Una validación respalda a lo sumo un reporte. Los NULL (reportes históricos) no colisionan entre sí.
	IF NOT EXISTS (
		SELECT 1 FROM pg_constraint
		WHERE conname = 'uq_reporte_validacion' AND conrelid = 'public.reportes_prospeccion'::regclass
	) THEN
		ALTER TABLE reportes_prospeccion ADD CONSTRAINT uq_reporte_validacion UNIQUE (validacion_id);
	END IF;

	IF NOT EXISTS (
		SELECT 1 FROM pg_constraint
		WHERE conname = 'fk_reporte_validacion' AND conrelid = 'public.reportes_prospeccion'::regclass
	) THEN
		ALTER TABLE reportes_prospeccion
			ADD CONSTRAINT fk_reporte_validacion FOREIGN KEY (validacion_id) REFERENCES analisis_gri_validaciones (id);
	END IF;

	-- 3. Fuera la unicidad por empresa/año: es lo que impedía una segunda emisión.
	IF EXISTS (
		SELECT 1 FROM pg_constraint
		WHERE conname = 'uq_reporte_empresa_anio' AND conrelid = 'public.reportes_prospeccion'::regclass
	) THEN
		ALTER TABLE reportes_prospeccion DROP CONSTRAINT uq_reporte_empresa_anio;
	END IF;
END
$$;

-- Los disparadores de solo inserción (tr_reportes_solo_insercion, migración 0005) no se tocan: cada versión sigue siendo inmutable.
