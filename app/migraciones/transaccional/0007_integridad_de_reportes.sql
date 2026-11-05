-- Migración 0007 (transaccional): integridad de las emisiones de reportes versionados (BUG-15, entrega 1).
-- La aplica `app/migraciones` al arrancar, una sola vez y en una transacción; no se ejecuta a mano. La 0006 NO se modifica: esta
-- migración suma las protecciones que le faltaban.
--
-- QUÉ AÑADE
--   1. `uq_analisis_validacion_id_empresa_anio` UNIQUE (id, empresa_id, anio) en `analisis_gri_validaciones`: redundante con la clave
--      primaria, pero necesaria como destino de la clave foránea compuesta. Es solo DDL: no toca filas de la tabla (de solo inserción).
--   2. `fk_reporte_validacion_empresa_anio` FOREIGN KEY (validacion_id, empresa_id, anio) REFERENCES analisis_gri_validaciones
--      (id, empresa_id, anio): la validación de un reporte DEBE ser de la misma empresa y el mismo año. Con MATCH SIMPLE (el valor por
--      defecto) no se evalúa cuando `validacion_id` es NULL, de modo que los reportes históricos siguen siendo válidos. La FK simple
--      `fk_reporte_validacion` de la 0006 se conserva (comprueba que la validación exista).
--   3. Disparador `tr_reportes_exigir_validacion` (BEFORE INSERT): un reporte NUEVO no puede insertarse sin `validacion_id`. Los
--      históricos conservan su NULL porque el disparador solo actúa al insertar y nunca se reinsertan ni se actualizan. No se
--      usa un CHECK porque rechazaría las filas históricas existentes.
--
-- SE CONSERVA lo ya protegido: UNIQUE(validacion_id) (una emisión por validación), UNIQUE(empresa_id, anio, version),
-- CHECK(version >= 1) y los disparadores de solo inserción de reportes y validaciones. No se desactiva ninguno, no se usa TRUNCATE ni
-- CASCADE y no se actualiza ni se elimina ninguna fila de reportes ni de validaciones.
--
-- DATOS INCOMPATIBLES. Si algún reporte ya tiene una validación de OTRA empresa o año (el servicio lo impedía, pero la base no),
-- la migración se detiene SIN modificar nada con el SQLSTATE `IG001` (el motor lo informa como `MIGRATION_DATA_INCOMPATIBLE`).
-- Para listarlos: SELECT r.id, r.empresa_id, r.anio, v.empresa_id AS validacion_empresa, v.anio AS validacion_anio
--                 FROM reportes_prospeccion r JOIN analisis_gri_validaciones v ON v.id = r.validacion_id
--                 WHERE v.empresa_id <> r.empresa_id OR v.anio <> r.anio;
-- Se resuelven a mano y con revisión; esta migración no los borra, no los corrige ni los reclasifica.
--
-- IDEMPOTENTE Y SIN OBJETOS DECLARADOS (igual que la 0005 y la 0006): `reportes_prospeccion` la crea `create_all` antes de las
-- migraciones y los disparadores no forman parte de la firma, así que no hay adopción posible. Requiere PostgreSQL >= 12 y las
-- migraciones 0004 a 0006.

DO $$
BEGIN
	IF EXISTS (
		SELECT 1 FROM reportes_prospeccion r
		JOIN analisis_gri_validaciones v ON v.id = r.validacion_id
		WHERE v.empresa_id <> r.empresa_id OR v.anio <> r.anio
	) THEN
		RAISE EXCEPTION 'Hay reportes cuya validacion_id pertenece a otra empresa o a otro anio; resuelvalos antes de migrar (ver la cabecera de la migracion 0007).'
			USING ERRCODE = 'IG001';
	END IF;

	IF NOT EXISTS (
		SELECT 1 FROM pg_constraint
		WHERE conname = 'uq_analisis_validacion_id_empresa_anio' AND conrelid = 'public.analisis_gri_validaciones'::regclass
	) THEN
		ALTER TABLE analisis_gri_validaciones
			ADD CONSTRAINT uq_analisis_validacion_id_empresa_anio UNIQUE (id, empresa_id, anio);
	END IF;

	IF NOT EXISTS (
		SELECT 1 FROM pg_constraint
		WHERE conname = 'fk_reporte_validacion_empresa_anio' AND conrelid = 'public.reportes_prospeccion'::regclass
	) THEN
		ALTER TABLE reportes_prospeccion
			ADD CONSTRAINT fk_reporte_validacion_empresa_anio FOREIGN KEY (validacion_id, empresa_id, anio)
			REFERENCES analisis_gri_validaciones (id, empresa_id, anio);
	END IF;
END
$$;

CREATE OR REPLACE FUNCTION igualab_exigir_validacion_en_reporte() RETURNS trigger AS $$
BEGIN
	IF NEW.validacion_id IS NULL THEN
		RAISE EXCEPTION 'Un reporte nuevo debe indicar la validacion GRI que lo respalda (validacion_id).'
			USING ERRCODE = 'not_null_violation', COLUMN = 'validacion_id', TABLE = 'reportes_prospeccion';
	END IF;
	RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_reportes_exigir_validacion ON reportes_prospeccion;
CREATE TRIGGER tr_reportes_exigir_validacion
	BEFORE INSERT ON reportes_prospeccion
	FOR EACH ROW EXECUTE FUNCTION igualab_exigir_validacion_en_reporte();
