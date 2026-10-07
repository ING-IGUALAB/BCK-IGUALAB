-- Operaciones de ingesta (identificador entregado ANTES de cargar el archivo). DDL PARA REVISIÓN:
-- no se ejecuta desde la aplicación ni desde el arranque. Aplicar DESPUÉS de la tabla `documentos`
-- (001_documentos.sql, o 001 + 002 si ya existía). Requiere las tablas `usuarios` y `documentos`.
-- Sin tipos enumerados nuevos: el estado es VARCHAR con CHECK.

CREATE TABLE operaciones_ingesta (
	id UUID NOT NULL, 
	ambiente VARCHAR(32) NOT NULL, 
	usuario_id UUID NOT NULL, 
	estado VARCHAR(16) DEFAULT 'CREADA' NOT NULL, 
	documento_id UUID, 
	creada_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	carga_iniciada_en TIMESTAMP WITH TIME ZONE, 
	carga_vigente_hasta TIMESTAMP WITH TIME ZONE, 
	actualizada_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	codigo_error VARCHAR(64), 
	mensaje_error VARCHAR(300), 
	estado_http INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_operaciones_estado_valido CHECK (estado IN ('CREADA', 'EN_CARGA', 'CON_DOCUMENTO', 'RECHAZADA')), 
	CONSTRAINT ck_operaciones_documento_solo_si_enlazada CHECK ((estado = 'CON_DOCUMENTO') = (documento_id IS NOT NULL)), 
	CONSTRAINT ck_operaciones_error_solo_si_rechazada CHECK ((estado = 'RECHAZADA') = (codigo_error IS NOT NULL)), 
	CONSTRAINT ck_operaciones_carga_solo_si_iniciada CHECK ((estado = 'CREADA') = (carga_iniciada_en IS NULL)), 
	CONSTRAINT ck_operaciones_vigencia_con_carga CHECK ((carga_iniciada_en IS NULL) = (carga_vigente_hasta IS NULL)), 
	FOREIGN KEY(usuario_id) REFERENCES usuarios (id), 
	FOREIGN KEY(documento_id) REFERENCES documentos (id)
);

CREATE INDEX ix_operaciones_ingesta_usuario_id ON operaciones_ingesta (usuario_id);

CREATE UNIQUE INDEX uq_operaciones_documento ON operaciones_ingesta (documento_id) WHERE documento_id IS NOT NULL;
