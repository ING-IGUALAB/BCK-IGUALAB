-- Migración 0005 (transaccional): auditoría con resultado y de solo inserción (LTX:RN-043, RN-044, RN-045; RNF-022) y reportes de
-- prospección inmutables (LTX:RN-041). La aplica `app/migraciones` al arrancar, una sola vez y en una transacción; no se ejecuta
-- a mano.
--
-- POR QUÉ ES IDEMPOTENTE Y NO DECLARA OBJETOS. `auditoria` y `reportes_prospeccion` las crea `create_all` ANTES de las migraciones
-- (módulos anteriores al motor). En una base ya desplegada tienen la forma antigua; en una instalación nueva `create_all` ya las
-- crea con la forma de los modelos actuales (columna `resultado`, restricción, valor del tipo enumerado). Los disparadores no forman
-- parte de la firma de esquema que compara el motor, así que adoptar por firma dejaría la tabla sin protección: por eso esta
-- migración se aplica siempre una vez y cada sentencia admite que el objeto ya exista.
--
-- Requiere PostgreSQL >= 12 (ALTER TYPE ... ADD VALUE dentro de una transacción) y las tablas `auditoria` y `reportes_prospeccion`.
-- No toca filas existentes: los eventos anteriores quedan con `resultado` nulo (no se deduce ni se inventa).

-- 1. Tipo de evento para el acceso denegado a la auditoría. SQLAlchemy guarda el NOMBRE del miembro del enumerado.
ALTER TYPE tipoeventoauditoria ADD VALUE IF NOT EXISTS 'ACCESO_DENEGADO';

-- 2. Resultado del evento (RN-043). Nulo solo en los eventos anteriores a esta migración.
ALTER TABLE auditoria ADD COLUMN IF NOT EXISTS resultado VARCHAR(16);

DO $$
BEGIN
	IF NOT EXISTS (
		SELECT 1 FROM pg_constraint
		WHERE conname = 'ck_auditoria_resultado_valido' AND conrelid = 'public.auditoria'::regclass
	) THEN
		ALTER TABLE auditoria
			ADD CONSTRAINT ck_auditoria_resultado_valido CHECK (resultado IS NULL OR resultado IN ('EXITOSO', 'FALLIDO', 'DENEGADO'));
	END IF;
END
$$;

-- 3. Solo inserción: un disparador rechaza UPDATE y DELETE de filas (RN-044, RN-041). Protege contra la aplicación y contra una sesión
-- SQL ordinaria; no sustituye los permisos del rol de la base (un propietario puede quitarlo o usar TRUNCATE).
CREATE OR REPLACE FUNCTION igualab_rechazar_modificacion_solo_insercion() RETURNS trigger AS $$
BEGIN
	RAISE EXCEPTION 'La tabla % es de solo inserción: no admite % de filas.', TG_TABLE_NAME, TG_OP
		USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_auditoria_solo_insercion ON auditoria;
CREATE TRIGGER tr_auditoria_solo_insercion
	BEFORE UPDATE OR DELETE ON auditoria
	FOR EACH ROW EXECUTE FUNCTION igualab_rechazar_modificacion_solo_insercion();

DROP TRIGGER IF EXISTS tr_reportes_solo_insercion ON reportes_prospeccion;
CREATE TRIGGER tr_reportes_solo_insercion
	BEFORE UPDATE OR DELETE ON reportes_prospeccion
	FOR EACH ROW EXECUTE FUNCTION igualab_rechazar_modificacion_solo_insercion();
