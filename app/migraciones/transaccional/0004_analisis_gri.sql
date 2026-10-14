-- Migración 0004 (transaccional): análisis y validación de brechas GRI (Módulo 5, CU005).
-- La aplica `app/migraciones` al arrancar, una sola vez y en una transacción; no se ejecuta a mano.
-- Requiere las tablas `usuarios` y `empresas`. No toca `documentos` ni `documentos.analisis`: la evidencia de ingesta
-- no se copia ni se modifica. Sin tipos enumerados nuevos: los estados son VARCHAR con CHECK.
--
-- Tablas:
--   analisis_gri_control        una fila por (ambiente, empresa, año); `version` cuenta los cambios de estado y se usa para
--                               el control optimista de concurrencia (la fila se bloquea con FOR UPDATE al escribir).
--   analisis_gri_estados        estado ACTUAL por identidad GRI (código + edición). Sin fila = sin estado asignado.
--   analisis_gri_historial      cambios de estado. SOLO INSERCIÓN: un disparador rechaza UPDATE y DELETE.
--   analisis_gri_validaciones   confirmaciones de «Validar análisis». SOLO INSERCIÓN, igual que el historial.
--
-- El disparador protege contra UPDATE/DELETE por la aplicación o por una sesión SQL ordinaria; no sustituye los permisos del
-- rol de la base (un propietario de la tabla puede quitarlo o usar TRUNCATE).

CREATE TABLE analisis_gri_control (
	ambiente VARCHAR(32) NOT NULL,
	empresa_id UUID NOT NULL,
	anio INTEGER NOT NULL,
	version INTEGER DEFAULT 0 NOT NULL,
	creado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	actualizado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (ambiente, empresa_id, anio),
	CONSTRAINT ck_analisis_control_anio_minimo CHECK (anio >= 2000),
	CONSTRAINT ck_analisis_control_version_no_negativa CHECK (version >= 0),
	FOREIGN KEY(empresa_id) REFERENCES empresas (id)
);

CREATE TABLE analisis_gri_estados (
	id UUID NOT NULL,
	ambiente VARCHAR(32) NOT NULL,
	empresa_id UUID NOT NULL,
	anio INTEGER NOT NULL,
	identidad VARCHAR(48) NOT NULL,
	codigo VARCHAR(3) NOT NULL,
	edicion VARCHAR(4),
	identidad_tipo VARCHAR(24) NOT NULL,
	estado VARCHAR(16) NOT NULL,
	version INTEGER DEFAULT 1 NOT NULL,
	asignado_por UUID NOT NULL,
	asignado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(ambiente, empresa_id, anio) REFERENCES analisis_gri_control (ambiente, empresa_id, anio),
	CONSTRAINT uq_analisis_estado_identidad UNIQUE (ambiente, empresa_id, anio, identidad),
	CONSTRAINT ck_analisis_estado_valido CHECK (estado IN ('OK', 'BAJA_SUSTANCIA', 'SUB_REPORTADO')),
	CONSTRAINT ck_analisis_estado_tipo_identidad CHECK (identidad_tipo IN ('EDICION_RESUELTA', 'EDICION_AMBIGUA', 'EDICION_NO_CATALOGADA')),
	CONSTRAINT ck_analisis_estado_version_positiva CHECK (version >= 1),
	FOREIGN KEY(asignado_por) REFERENCES usuarios (id)
);

CREATE INDEX ix_analisis_gri_estados_asignado_por ON analisis_gri_estados (asignado_por);

CREATE TABLE analisis_gri_historial (
	id UUID NOT NULL,
	ambiente VARCHAR(32) NOT NULL,
	empresa_id UUID NOT NULL,
	anio INTEGER NOT NULL,
	identidad VARCHAR(48) NOT NULL,
	codigo VARCHAR(3) NOT NULL,
	edicion VARCHAR(4),
	identidad_tipo VARCHAR(24) NOT NULL,
	estado_anterior VARCHAR(16),
	estado_nuevo VARCHAR(16) NOT NULL,
	usuario_id UUID NOT NULL,
	cambiado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	version_analisis INTEGER NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_analisis_historial_estado_nuevo CHECK (estado_nuevo IN ('OK', 'BAJA_SUSTANCIA', 'SUB_REPORTADO')),
	CONSTRAINT ck_analisis_historial_estado_anterior CHECK (estado_anterior IS NULL OR estado_anterior IN ('OK', 'BAJA_SUSTANCIA', 'SUB_REPORTADO')),
	CONSTRAINT ck_analisis_historial_es_un_cambio CHECK (estado_anterior IS DISTINCT FROM estado_nuevo),
	CONSTRAINT ck_analisis_historial_tipo_identidad CHECK (identidad_tipo IN ('EDICION_RESUELTA', 'EDICION_AMBIGUA', 'EDICION_NO_CATALOGADA')),
	CONSTRAINT ck_analisis_historial_version_positiva CHECK (version_analisis >= 1),
	FOREIGN KEY(empresa_id) REFERENCES empresas (id),
	FOREIGN KEY(usuario_id) REFERENCES usuarios (id)
);

CREATE INDEX ix_analisis_gri_historial_usuario_id ON analisis_gri_historial (usuario_id);

CREATE INDEX ix_analisis_historial_consulta ON analisis_gri_historial (ambiente, empresa_id, anio, cambiado_en);

CREATE TABLE analisis_gri_validaciones (
	id UUID NOT NULL,
	ambiente VARCHAR(32) NOT NULL,
	empresa_id UUID NOT NULL,
	anio INTEGER NOT NULL,
	numero INTEGER NOT NULL,
	version_estados INTEGER NOT NULL,
	huella_documentos VARCHAR(64) NOT NULL,
	documentos JSONB NOT NULL,
	estados JSONB NOT NULL,
	puntaje_suma INTEGER NOT NULL,
	puntaje_filas INTEGER NOT NULL,
	validado_por UUID NOT NULL,
	validado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(ambiente, empresa_id, anio) REFERENCES analisis_gri_control (ambiente, empresa_id, anio),
	CONSTRAINT uq_analisis_validacion_numero UNIQUE (ambiente, empresa_id, anio, numero),
	CONSTRAINT ck_analisis_validacion_numero_positivo CHECK (numero >= 1),
	CONSTRAINT ck_analisis_validacion_version_positiva CHECK (version_estados >= 1),
	CONSTRAINT ck_analisis_validacion_huella CHECK (length(huella_documentos) = 64),
	CONSTRAINT ck_analisis_validacion_puntaje CHECK (puntaje_filas >= 1 AND puntaje_suma >= 0 AND puntaje_suma <= 100 * puntaje_filas),
	CONSTRAINT ck_analisis_validacion_documentos_arreglo CHECK (jsonb_typeof(documentos) = 'array'),
	CONSTRAINT ck_analisis_validacion_estados_arreglo CHECK (jsonb_typeof(estados) = 'array'),
	FOREIGN KEY(validado_por) REFERENCES usuarios (id)
);

CREATE INDEX ix_analisis_gri_validaciones_validado_por ON analisis_gri_validaciones (validado_por);

-- Solo inserción: el historial y las validaciones no se editan ni se borran.
CREATE FUNCTION igualab_rechazar_modificacion_analisis() RETURNS trigger AS $$
BEGIN
	RAISE EXCEPTION 'La tabla % es de solo inserción: no admite % de filas.', TG_TABLE_NAME, TG_OP
		USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tr_analisis_historial_solo_insercion
	BEFORE UPDATE OR DELETE ON analisis_gri_historial
	FOR EACH ROW EXECUTE FUNCTION igualab_rechazar_modificacion_analisis();

CREATE TRIGGER tr_analisis_validaciones_solo_insercion
	BEFORE UPDATE OR DELETE ON analisis_gri_validaciones
	FOR EACH ROW EXECUTE FUNCTION igualab_rechazar_modificacion_analisis();
