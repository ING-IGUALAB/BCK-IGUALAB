-- Migración 0010 (transaccional): historial del Asistente IA, ENTREGA 2 (continuar una conversación guardada y persistir los nuevos
-- intercambios; Módulo 6, CU004). La aplica `app/migraciones` al arrancar, una sola vez y en una transacción; no se ejecuta a mano.
-- Requiere la 0009 (`conversaciones_asistente`, `mensajes_asistente`). NO edita la 0009: la amplía.
--
-- 1. PROCEDENCIA de `mensajes_asistente`. Se amplía el CHECK `ck_mensajes_asistente_procedencia` (mismo nombre) y se liga el valor al
--    rol, siempre fijado por el SERVIDOR:
--      APORTADO_POR_CLIENTE   historial importado por «Guardar conversación» (entrega 1), de cualquier rol. Las filas existentes
--                             conservan este valor: NO se reclasifican retroactivamente.
--      ENVIADO_POR_USUARIO    solo `usuario`: la pregunta que el usuario envió a esta conversación.
--      GENERADO_POR_SERVIDOR  solo `asistente`: la respuesta que el RAG del servidor generó, verificó y persistió.
--    Todas las filas existentes (APORTADO_POR_CLIENTE, rol usuario o asistente) cumplen el nuevo CHECK.
--
-- 2. `intercambios_asistente`: reserva duradera y breve de UNA consulta de una conversación, con estado, vencimiento y token de
--    propiedad de la ejecución. Coordina la idempotencia y la exclusión entre procesos SIN mantener transacciones ni bloqueos del
--    historial mientras se llama a los proveedores de IA. Estados:
--      EN_CURSO     reservada; la ejecución que posee `token_ejecucion` está consultando al RAG.
--      COMPLETADO   pregunta y respuesta publicadas (ambas referenciadas aquí).
--      FALLIDO      la ejecución falló de forma recuperable; la misma clave y la misma pregunta pueden reintentarse.
--      VENCIDO      una reserva EN_CURSO vencida que otra solicitud sustituyó.
--    Garantías de la base:
--      · UNIQUE (conversacion_id, clave_idempotencia): como máximo un intercambio por clave, también ante solicitudes simultáneas.
--      · índice único PARCIAL sobre (conversacion_id) WHERE estado = 'EN_CURSO': una sola consulta en curso por conversación.
--      · COMPLETADO <=> los dos mensajes publicados (CHECK), y cada mensaje pertenece a lo sumo a un intercambio (UNIQUE).
--      · `huella_pregunta` (SHA-256) distingue un reintento (misma pregunta) de un conflicto (otra pregunta con la misma clave).
--    La pregunta NO se guarda aquí: solo vive en `mensajes_asistente` cuando el intercambio se completa.
--    `conversaciones_asistente.huella_contenido` y `.clave_idempotencia` (guardado inicial) no se modifican nunca.

ALTER TABLE mensajes_asistente DROP CONSTRAINT ck_mensajes_asistente_procedencia;
ALTER TABLE mensajes_asistente ADD CONSTRAINT ck_mensajes_asistente_procedencia CHECK (
	(rol = 'usuario' AND procedencia IN ('APORTADO_POR_CLIENTE', 'ENVIADO_POR_USUARIO'))
	OR (rol = 'asistente' AND procedencia IN ('APORTADO_POR_CLIENTE', 'GENERADO_POR_SERVIDOR'))
);

CREATE TABLE intercambios_asistente (
	id UUID NOT NULL,
	conversacion_id UUID NOT NULL,
	clave_idempotencia UUID NOT NULL,
	huella_pregunta VARCHAR(64) NOT NULL,
	estado VARCHAR(16) NOT NULL,
	token_ejecucion UUID NOT NULL,
	intentos INTEGER DEFAULT 1 NOT NULL,
	vence_en TIMESTAMP WITH TIME ZONE NOT NULL,
	codigo_error VARCHAR(64),
	mensaje_pregunta_id UUID,
	mensaje_respuesta_id UUID,
	creado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	actualizado_en TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	completado_en TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	FOREIGN KEY(conversacion_id) REFERENCES conversaciones_asistente (id),
	FOREIGN KEY(mensaje_pregunta_id) REFERENCES mensajes_asistente (id),
	FOREIGN KEY(mensaje_respuesta_id) REFERENCES mensajes_asistente (id),
	CONSTRAINT uq_intercambios_asistente_clave UNIQUE (conversacion_id, clave_idempotencia),
	CONSTRAINT uq_intercambios_asistente_mensaje_pregunta UNIQUE (mensaje_pregunta_id),
	CONSTRAINT uq_intercambios_asistente_mensaje_respuesta UNIQUE (mensaje_respuesta_id),
	CONSTRAINT ck_intercambios_asistente_estado CHECK (estado IN ('EN_CURSO', 'COMPLETADO', 'FALLIDO', 'VENCIDO')),
	CONSTRAINT ck_intercambios_asistente_huella CHECK (huella_pregunta ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_intercambios_asistente_intentos CHECK (intentos >= 1),
	CONSTRAINT ck_intercambios_asistente_mensajes CHECK (
		(estado = 'COMPLETADO' AND mensaje_pregunta_id IS NOT NULL AND mensaje_respuesta_id IS NOT NULL)
		OR (estado <> 'COMPLETADO' AND mensaje_pregunta_id IS NULL AND mensaje_respuesta_id IS NULL)
	),
	CONSTRAINT ck_intercambios_asistente_completado_en CHECK ((estado = 'COMPLETADO') = (completado_en IS NOT NULL)),
	CONSTRAINT ck_intercambios_asistente_error CHECK (codigo_error IS NULL OR estado = 'FALLIDO'),
	CONSTRAINT ck_intercambios_asistente_fechas CHECK (actualizado_en >= creado_en)
);

-- Una sola consulta en curso por conversación (entre procesos e instancias: lo decide PostgreSQL).
CREATE UNIQUE INDEX uq_intercambios_asistente_en_curso ON intercambios_asistente (conversacion_id) WHERE estado = 'EN_CURSO';
