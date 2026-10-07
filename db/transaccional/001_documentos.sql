-- Documentos de ingesta (Etapa 4A + coordinador). DDL de INSTALACIÓN NUEVA, PARA REVISIÓN:
-- no se ejecuta desde la aplicación. Una tabla ya creada con la Etapa 4A se actualiza con
-- db/transaccional/002_documentos_coordinador.sql (no use ambos sobre la misma base).
-- Requiere las tablas `empresas` y `usuarios` y el tipo `sector_empresa` (ya existentes).

CREATE TYPE tipo_documento AS ENUM ('MEMORIA_ANUAL', 'REPORTE_SOSTENIBILIDAD_GRI');

CREATE TYPE estado_procesamiento AS ENUM ('EN_PROCESO', 'COMPLETADO', 'FALLIDO');

CREATE TYPE resultado_analisis AS ENUM ('CON_HALLAZGOS', 'OBSERVADO');

CREATE TYPE estado_compensacion AS ENUM ('NINGUNA', 'PENDIENTE', 'COMPLETADA');

CREATE TABLE documentos (
	id UUID NOT NULL, 
	ambiente VARCHAR(32) NOT NULL, 
	empresa_id UUID NOT NULL, 
	anio INTEGER NOT NULL, 
	tipo tipo_documento NOT NULL, 
	sector sector_empresa NOT NULL, 
	nombre_archivo VARCHAR(255) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	tamano_bytes BIGINT NOT NULL, 
	usuario_id UUID NOT NULL, 
	creado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	clave_original VARCHAR(512) NOT NULL, 
	version_id_original VARCHAR(255), 
	almacenamiento_intentado_en TIMESTAMP WITH TIME ZONE, 
	original_almacenado_en TIMESTAMP WITH TIME ZONE, 
	estado_procesamiento estado_procesamiento DEFAULT 'EN_PROCESO' NOT NULL, 
	resultado_analisis resultado_analisis, 
	completado_en TIMESTAMP WITH TIME ZONE, 
	motivo_fallo VARCHAR(64), 
	fallido_en TIMESTAMP WITH TIME ZONE, 
	estado_compensacion estado_compensacion DEFAULT 'NINGUNA' NOT NULL, 
	compensacion_intentos INTEGER DEFAULT 0 NOT NULL, 
	ultimo_error_compensacion VARCHAR(64), 
	compensada_en TIMESTAMP WITH TIME ZONE, 
	ejecucion_token UUID NOT NULL, 
	ejecucion_vigente_hasta TIMESTAMP WITH TIME ZONE NOT NULL, 
	reserva_activa BOOLEAN DEFAULT true NOT NULL, 
	analisis JSONB, 
	etapa_actual VARCHAR(32) DEFAULT 'RESERVADO' NOT NULL, 
	fragmentos_procesados INTEGER DEFAULT 0 NOT NULL, 
	fragmentos_total INTEGER, 
	progreso_actualizado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	advertencias JSONB DEFAULT '[]' NOT NULL, 
	vector_escritura_intentada_en TIMESTAMP WITH TIME ZONE, 
	vector_publicado_en TIMESTAMP WITH TIME ZONE, 
	vector_publicacion_intentos INTEGER DEFAULT 0 NOT NULL, 
	vector_ultimo_error VARCHAR(64), 
	PRIMARY KEY (id), 
	CONSTRAINT ck_documentos_anio_minimo CHECK (anio >= 2000), 
	CONSTRAINT ck_documentos_sha256_longitud CHECK (length(sha256) = 64), 
	CONSTRAINT ck_documentos_tamano_positivo CHECK (tamano_bytes > 0), 
	CONSTRAINT ck_documentos_nombre_no_vacio CHECK (length(trim(nombre_archivo)) > 0), 
	CONSTRAINT ck_documentos_resultado_solo_si_completado CHECK ((estado_procesamiento = 'COMPLETADO') = (resultado_analisis IS NOT NULL)), 
	CONSTRAINT ck_documentos_completado_en CHECK ((estado_procesamiento = 'COMPLETADO') = (completado_en IS NOT NULL)), 
	CONSTRAINT ck_documentos_completado_con_original CHECK (estado_procesamiento <> 'COMPLETADO' OR original_almacenado_en IS NOT NULL), 
	CONSTRAINT ck_documentos_fallido_con_motivo CHECK (estado_procesamiento <> 'FALLIDO' OR (motivo_fallo IS NOT NULL AND fallido_en IS NOT NULL)), 
	CONSTRAINT ck_documentos_compensacion_solo_si_fallido CHECK (estado_compensacion = 'NINGUNA' OR estado_procesamiento = 'FALLIDO'), 
	CONSTRAINT ck_documentos_reserva_solo_liberada_si_limpio CHECK (reserva_activa OR (estado_procesamiento = 'FALLIDO' AND estado_compensacion IN ('NINGUNA', 'COMPLETADA'))), 
	CONSTRAINT ck_documentos_intentos_no_negativos CHECK (compensacion_intentos >= 0), 
	CONSTRAINT ck_documentos_etapa_valida CHECK (etapa_actual IN ('RESERVADO', 'ALMACENANDO_ORIGINAL', 'INDEXANDO', 'ANALIZANDO', 'COMPLETANDO', 'PUBLICANDO', 'FINALIZADO')), 
	CONSTRAINT ck_documentos_fragmentos_progreso CHECK (fragmentos_procesados >= 0 AND (fragmentos_total IS NULL OR (fragmentos_total >= 0 AND fragmentos_procesados <= fragmentos_total))), 
	CONSTRAINT ck_documentos_publicacion_intentos CHECK (vector_publicacion_intentos >= 0), 
	CONSTRAINT ck_documentos_vector_publicado_solo_completado CHECK (vector_publicado_en IS NULL OR estado_procesamiento = 'COMPLETADO'), 
	CONSTRAINT ck_documentos_vector_publicado_con_intento CHECK (vector_publicado_en IS NULL OR vector_escritura_intentada_en IS NOT NULL), 
	CONSTRAINT ck_documentos_sin_analisis_si_fallido CHECK (analisis IS NULL OR estado_procesamiento <> 'FALLIDO'), 
	CONSTRAINT ck_documentos_analisis_consistente CHECK (resultado_analisis IS NULL OR analisis IS NULL OR analisis->>'resultado' = resultado_analisis::text), 
	CONSTRAINT ck_documentos_analisis_estructura CHECK (analisis IS NULL OR (jsonb_typeof(analisis) = 'object' AND coalesce(analisis->>'resultado', '') IN ('CON_HALLAZGOS', 'OBSERVADO') AND length(coalesce(analisis->>'version_catalogo', '')) > 0 AND coalesce(jsonb_typeof(analisis->'motivos'), '') = 'array' AND coalesce(jsonb_typeof(analisis->'gri'), '') = 'array' AND coalesce(jsonb_typeof(analisis->'sanciones'), '') = 'array' AND coalesce(jsonb_typeof(analisis->'advertencias'), '') = 'array')), 
	CONSTRAINT ck_documentos_advertencias_arreglo CHECK (jsonb_typeof(advertencias) = 'array'), 
	FOREIGN KEY(empresa_id) REFERENCES empresas (id), 
	FOREIGN KEY(usuario_id) REFERENCES usuarios (id), 
	UNIQUE (clave_original)
);

CREATE INDEX ix_documentos_empresa_id ON documentos (empresa_id);

CREATE INDEX ix_documentos_usuario_id ON documentos (usuario_id);

CREATE UNIQUE INDEX uq_documentos_empresa_anio_tipo_activo ON documentos (ambiente, empresa_id, anio, tipo) WHERE reserva_activa;

CREATE UNIQUE INDEX uq_documentos_sha256_activo ON documentos (ambiente, sha256) WHERE reserva_activa;
