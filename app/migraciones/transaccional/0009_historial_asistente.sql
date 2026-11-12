-- Migración 0009 (transaccional): historial del Asistente IA, ENTREGA 1 (guardar y recuperar conversaciones; Módulo 6, CU004).
-- La aplica `app/migraciones` al arrancar, una sola vez y en una transacción; no se ejecuta a mano.
-- Requiere las tablas `usuarios` y `empresas` y el tipo `sector_empresa` (ya existentes). No toca ninguna tabla existente.
--
-- Tablas:
--   conversaciones_asistente   una conversación = un propietario, un ambiente, UNA empresa, UN sector y UN año. La unicidad de
--                              (ambiente, usuario_id, clave_idempotencia) hace idempotente el guardado inicial también ante
--                              solicitudes simultáneas; `huella_contenido` (SHA-256) distingue un reintento de un conflicto.
--   mensajes_asistente         mensajes con orden explícito, único por conversación. `datos_respuesta` guarda la respuesta
--                              estructurada del asistente (citas, afirmaciones, ausencia de evidencia) y solo existe en los
--                              mensajes del asistente.
--
-- PROCEDENCIA. `mensajes_asistente.procedencia` la fija el servidor. Hoy el único valor es APORTADO_POR_CLIENTE: el asistente no
-- conserva lo que responde y el historial lo aporta el frontend; validar su estructura no demuestra que lo generó el backend ni
-- que sus citas se verificaron originalmente. Las entregas siguientes ampliarán este CHECK con una migración nueva.
--
-- Sin tipos enumerados nuevos (rol y procedencia son VARCHAR con CHECK). Sin eliminación de conversaciones en esta entrega: las
-- claves foráneas no declaran ON DELETE.

CREATE TABLE conversaciones_asistente (
	id UUID NOT NULL,
	ambiente VARCHAR(32) NOT NULL,
	usuario_id UUID NOT NULL,
	empresa_id UUID NOT NULL,
	sector sector_empresa NOT NULL,
	anio INTEGER NOT NULL,
	titulo VARCHAR(80) NOT NULL,
	clave_idempotencia UUID NOT NULL,
	huella_contenido VARCHAR(64) NOT NULL,
	creada_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	actualizada_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(usuario_id) REFERENCES usuarios (id),
	FOREIGN KEY(empresa_id) REFERENCES empresas (id),
	CONSTRAINT uq_conversaciones_asistente_idempotencia UNIQUE (ambiente, usuario_id, clave_idempotencia),
	CONSTRAINT ck_conversaciones_asistente_anio CHECK (anio BETWEEN 2000 AND 9999),
	CONSTRAINT ck_conversaciones_asistente_titulo_no_vacio CHECK (length(trim(titulo)) > 0),
	CONSTRAINT ck_conversaciones_asistente_huella CHECK (huella_contenido ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_conversaciones_asistente_fechas CHECK (actualizada_en >= creada_en)
);

-- Listado del propietario, de la más reciente a la más antigua, con desempate estable.
CREATE INDEX ix_conversaciones_asistente_listado ON conversaciones_asistente (ambiente, usuario_id, actualizada_en DESC, id DESC);

CREATE TABLE mensajes_asistente (
	id UUID NOT NULL,
	conversacion_id UUID NOT NULL,
	orden INTEGER NOT NULL,
	rol VARCHAR(16) NOT NULL,
	contenido TEXT NOT NULL,
	datos_respuesta JSONB,
	procedencia VARCHAR(32) NOT NULL,
	creado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(conversacion_id) REFERENCES conversaciones_asistente (id),
	CONSTRAINT uq_mensajes_asistente_orden UNIQUE (conversacion_id, orden),
	CONSTRAINT ck_mensajes_asistente_orden_positivo CHECK (orden >= 1),
	CONSTRAINT ck_mensajes_asistente_rol CHECK (rol IN ('usuario', 'asistente')),
	CONSTRAINT ck_mensajes_asistente_contenido_no_vacio CHECK (length(trim(contenido)) > 0),
	CONSTRAINT ck_mensajes_asistente_datos_solo_asistente CHECK ((rol = 'asistente') = (datos_respuesta IS NOT NULL)),
	CONSTRAINT ck_mensajes_asistente_datos_objeto CHECK (datos_respuesta IS NULL OR jsonb_typeof(datos_respuesta) = 'object'),
	CONSTRAINT ck_mensajes_asistente_procedencia CHECK (procedencia IN ('APORTADO_POR_CLIENTE'))
);
