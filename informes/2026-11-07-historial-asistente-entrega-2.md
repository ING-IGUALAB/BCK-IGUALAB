# Historial del Asistente IA — Entrega 2: continuar conversaciones guardadas

Fecha: 2026-11-07. **Incluye el ajuste 2.1** (recursos vectoriales diferidos, presupuesto global de generación y documentación del error de respuesta no persistible). Estado: implementado y verificado en local; sin staging, commit, push ni despliegue. La migración 0010 **no** se aplicó a QA, UAT ni producción. Complementa el [informe de la entrega 1](2026-11-07-historial-asistente-entrega-1.md), que no se repite aquí (modelo base, guardado inicial, listado, recuperación, procedencia de lo importado).

> **Alcance.** Esta entrega **conserva la conversación y añade respuestas, pero todavía no interpreta preguntas de seguimiento usando el historial.** Cada pregunta se responde de forma independiente con el RAG actual, dentro de la empresa, el sector y el año de la conversación. Los mensajes anteriores no se envían al modelo, no se reformulan preguntas y no se generan resúmenes. *(Esa limitación era la de la entrega 2; la entrega 3 añadió la interpretación de seguimientos: ver [su informe](2026-11-07-historial-asistente-entrega-3.md).)*

## A. Solución aplicada

### Flujo

`POST /asistente/conversaciones/{conversacion_id}/consultas` (solo Administrador) trabaja en tres fases. **Ninguna transacción ni bloqueo del historial permanece abierto mientras se llama a embeddings o al LLM.**

| Fase | Qué ocurre | Transacción |
|---|---|---|
| 1. Reservar | Autoriza (propietario y ambiente salen de la sesión y la configuración; 404 uniforme), resuelve la idempotencia, aplica los límites que ya se pueden decidir, revalida empresa activa y sector, **solo para una consulta nueva** prepara los recursos del RAG (esquema vectorial, configuración vectorial, proveedores) y escribe la reserva (`EN_CURSO`, token nuevo, vencimiento) | corta, termina con `commit` |
| 2. RAG | `responder_consulta` **sin cambios** (misma verificación de afirmaciones y citas, mismas protecciones), con la pregunta sola y el contexto de la conversación, con la sesión vectorial abierta de forma diferida (cierre garantizado) y bajo el **presupuesto global de 240 s** | ninguna del historial |
| 3. Publicar | Bloquea la reserva **solo si el token sigue siendo suyo**; comprueba los límites con el tamaño real; inserta pregunta y respuesta con su orden definitivo; marca el intercambio `COMPLETADO` y actualiza `actualizada_en` | corta, todo o nada |

#### Orden real de autorización, idempotencia y recursos externos

La ruta **no declara** como dependencias el esquema vectorial, la sesión vectorial ni los proveedores. Recibe un `RecursosRag` diferido (`get_recursos_rag`): crearlo no valida, construye, conecta ni abre nada. El orden es:

| # | Paso | Resultado si falla |
|---|---|---|
| 1 | Autenticación y rol Administrador | 401 / 403 |
| 2 | Esquema del historial listo (solo base transaccional) | 503 `ASSISTANT_HISTORY_SCHEMA_NOT_READY` |
| 3 | La conversación es del usuario y del ambiente | 404 `CONVERSATION_NOT_FOUND` uniforme, **aunque lo vectorial esté ausente** |
| 4 | Idempotencia de la clave: otra pregunta → 409; en curso → 409; **completada → 200 con lo almacenado, desde la base transaccional** | ver Idempotencia |
| 5 | Solo una consulta nueva: otra consulta en curso, límites, empresa activa y sector | 409 / 400 |
| 6 | `RecursosRag.preparar()`: esquema vectorial listo, `VECTOR_DATABASE_URL` válida (solo se valida, no se conecta) y proveedores de embeddings y chat | 503 (`INGESTION_NOT_CONFIGURED` o el motivo del arranque, `EMBEDDINGS_NOT_CONFIGURED`, `LLM_NOT_CONFIGURED`) o 502 (`VECTOR_DATABASE_NOT_CONFIGURED`, `VECTOR_DATABASE_URL_INVALID`), como `POST /asistente/consultas`; **antes de reservar**: no queda nada que recuperar |
| 7 | Reserva (commit) | |
| 8 | Se abre la sesión vectorial (`async with`, cierre garantizado en éxito, fallo y cancelación) y se ejecuta el RAG bajo el presupuesto | un fallo aquí ocurre **después** de reservar: la reserva pasa a `FALLIDO` y la misma clave se recupera |

Por tanto una respuesta ya guardada sigue accesible por reintento aunque el esquema vectorial no esté preparado, `VECTOR_DATABASE_URL` falte o sea inválida, o los embeddings y el LLM no estén configurados: no se prepara, valida, abre ni construye nada de eso. Se reutilizan las comprobaciones y fábricas existentes (`esquema_vectorial_listo`, `url_vectorial`, `fabrica_sesiones_vectoriales`, `get_proveedor_*`); no se duplica la gestión de motores ni se consumen dependencias generadoras a mano. `POST /asistente/consultas` y las rutas de guardar, listar y recuperar no cambian.

### Modelo y migración 0010

La migración `0010_continuacion_asistente.sql` no edita la 0009 ni ninguna anterior; sus checksums no cambian.

**Procedencia** (`ck_mensajes_asistente_procedencia`, mismo nombre, ampliado y ligado al rol; siempre la fija el servidor):

| Valor | Rol | Significado |
|---|---|---|
| `APORTADO_POR_CLIENTE` | cualquiera | historial importado en la entrega 1; **no se eleva retroactivamente** |
| `ENVIADO_POR_USUARIO` | `usuario` | pregunta enviada a esta conversación |
| `GENERADO_POR_SERVIDOR` | `asistente` | respuesta que el RAG del servidor generó, verificó y persistió |

**`intercambios_asistente`** (reserva duradera y breve; no guarda la pregunta, solo su huella SHA-256):

| Campo | Uso |
|---|---|
| `conversacion_id`, `clave_idempotencia` | `UNIQUE`: como máximo un intercambio por clave y conversación |
| `huella_pregunta` | distingue un reintento de un conflicto |
| `estado`, `token_ejecucion`, `vence_en`, `intentos`, `codigo_error` | estado, propiedad de la ejecución, vencimiento (reloj de la base) y diagnóstico sin contenido |
| `mensaje_pregunta_id`, `mensaje_respuesta_id` | los dos mensajes publicados; `COMPLETADO` ⇔ ambos presentes (CHECK) y cada mensaje pertenece a lo sumo a un intercambio (`UNIQUE`) |

Un índice único **parcial** `(conversacion_id) WHERE estado = 'EN_CURSO'` garantiza una sola consulta en curso por conversación. El orden definitivo lo asigna la fase 3 (último `orden` + 1 y + 2) y lo protege `UNIQUE (conversacion_id, orden)` de la entrega 1.

### Estados de la reserva

| Estado | Significado | Sale de él cuando |
|---|---|---|
| `EN_CURSO` | la ejecución que posee el token consulta al RAG | publica (`COMPLETADO`), falla (`FALLIDO`) o vence y otra solicitud la sustituye (`VENCIDO`) |
| `COMPLETADO` | pregunta y respuesta publicadas | final |
| `FALLIDO` | falló de forma recuperable (proveedor, límite, persistencia) | la misma clave y pregunta la recuperan con un token nuevo (`intentos` + 1) |
| `VENCIDO` | reserva `EN_CURSO` vencida que sustituyó otra consulta | la misma clave y pregunta pueden reintentarse |

### Plazos: reserva y presupuesto global de la generación

**Qué existe en el código (verificado).** Embeddings: una sola llamada con `asyncio.timeout` de 120 s, sin reintentos (el cliente OCI usa `NoneRetryStrategy`; su conexión y lectura tienen plazos propios, y el SDK corre en un hilo). LLM: `httpx` con 60 s **por fase de red** (conexión, lectura, escritura, espera de conexión), **no** en total, sin reintentos. Búsqueda vectorial, visibilidad transaccional y espera de conexiones de los pools: sin plazo propio. **El RAG, tal como estaba, no tenía ningún máximo total**; un timeout individual de red no es un plazo global.

**Lo que se añadió.** `PRESUPUESTO_GENERACION_SEGUNDOS = 240`: un único `asyncio.timeout` que envuelve **todo** `responder_consulta` (embeddings de la consulta, búsqueda vectorial, visibilidad transaccional, LLM y verificación de citas). **No cubre** la fase de reserva ni la de publicación (transacciones cortas de base de datos sin plazo propio) ni el marcado del fallo: entre el presupuesto y la reserva (`PLAZO_RESERVA_SEGUNDOS = 300`, **sin cambios**: no se alargó para ocultar la falta de límite) queda una holgura de 60 s. **Esa holgura es un margen de diseño, NO un timeout: no impone ningún plazo a la reserva ni a la publicación**, que siguen sin plazo propio. Una prueba exige que el presupuesto sea al menos 30 s menor que la reserva. Sin heartbeat, workers, Redis ni dependencias nuevas.

**Al agotarse:** 504 `ASSISTANT_GENERATION_TIMEOUT` (con `details.presupuesto_segundos`); no se publica ningún mensaje; la reserva pasa a `FALLIDO` si sigue siendo de esa ejecución (token); la misma clave se puede reintentar; la sesión vectorial se cierra. Un `TimeoutError` ajeno al presupuesto no se reinterpreta como tal.

**Cancelar una espera no garantiza detener el trabajo externo ya iniciado** (por ejemplo, la llamada a OCI corre en un hilo y el proveedor puede terminar). La garantía sigue siendo **como máximo un intercambio persistido por clave**, no exactamente una llamada al proveedor.

**Reserva vencida.** Una reserva vencida que nadie recuperó ni sustituyó **sigue pudiendo confirmar** si conserva su token: decide el token, no el reloj (semántica sin cambios). Con el presupuesto global esto es compatible: una ejecución normal termina o falla antes de los 300 s; una reserva solo puede vencer con la ejecución aún viva si las fases cortas fuera del presupuesto superan la holgura de 60 s (que no se hace cumplir con ningún timeout), y en ese caso rige el token.

### Idempotencia de cada consulta

La clave se delimita por conversación dentro de la autorización de propietario y ambiente.

| Situación | Resultado |
|---|---|
| Misma clave y misma pregunta, **completada** | **200**, `reutilizado: true`, exactamente lo almacenado; sin proveedores, sin revalidar la empresa |
| Misma clave y otra pregunta | **409** `QUERY_IDEMPOTENCY_CONFLICT`, sin llamadas |
| Misma clave, todavía en curso | **409** `CONVERSATION_QUERY_IN_PROGRESS` con `Retry-After`; no inicia otra ejecución |
| Otra clave mientras hay una consulta en curso | **409** `CONVERSATION_BUSY` con `Retry-After`; no invoca a la IA ni se encola |
| Misma clave tras un fallo recuperable o una reserva vencida | se ejecuta de nuevo con la misma clave |

**Garantías.** Hay **como máximo un intercambio persistido por clave**. **No se promete exactamente una llamada externa:** si el proceso cae o se pierde la conexión después de llamar al proveedor y antes de publicar, el reintento vuelve a consultar al RAG (el proveedor no ofrece idempotencia).

### Concurrencia

Se decide en PostgreSQL, entre procesos e instancias, sin mutex en memoria, colas ni Redis: restricciones únicas (clave e índice parcial), actualizaciones condicionadas por `(estado, token)` y `SELECT … FOR UPDATE` de la reserva solo en la fase 3. Quien pierde una carrera captura la violación, deshace y resuelve con lo ya confirmado. Conversaciones distintas no se serializan entre sí.

### Guardado inicial de la entrega 1

`huella_contenido` y `clave_idempotencia` de la conversación **no se modifican** al añadir mensajes. Reintentar el `POST /asistente/conversaciones` original sigue siendo idempotente: responde 200 con `reutilizada: true` y describe la conversación que existe **ahora** (`total_mensajes`, todos los mensajes con sus procedencias). No restaura ni sobrescribe los mensajes antiguos ni elimina los nuevos.

### Límites

Se conservan los de la entrega 1 como máximos de la **conversación completa**: 40 mensajes y 1 000 000 bytes de contenido canónico, con la misma definición de tamaño que el guardado inicial (se mide lo recuperado de la base con la función canónica de la entrega 1; hay una prueba que lo compara). Una conversación de 40 mensajes se lee pero no admite otro par. Los límites de la pregunta (2 000 caracteres) y el contrato del asistente no cambian. No hay paginación de mensajes.

| Control | Cuándo | Error |
|---|---|---|
| 40 mensajes | antes de llamar a los proveedores y otra vez al publicar | 409 `CONVERSATION_MESSAGE_LIMIT_REACHED` |
| Tamaño con la pregunta | antes de los proveedores | 409 `CONVERSATION_SIZE_LIMIT_REACHED` |
| Tamaño real del par | al publicar | 409 `CONVERSATION_SIZE_LIMIT_REACHED`; no se publica ninguno de los dos mensajes y nada se trunca |

**Respuestas del RAG y el esquema.** Las cotas por campo de la entrega 1 (texto 20 000, afirmación 2 000, sección 1 000, nombre 255…) validan el historial **aportado por el cliente**. No se aplican a la respuesta que genera el servidor: se persiste `ConsultaResponse` íntegra en `datos_respuesta` (JSONB sin límite por campo) y solo rige el tamaño total. Se detectaron dos respuestas válidas del RAG que PostgreSQL no puede guardar: contenido vacío y el carácter NUL. En ese caso no se altera ni se inventa nada: el intercambio queda `FALLIDO` (`ASSISTANT_RESPONSE_NOT_PERSISTABLE`, 500) y no se guarda ningún mensaje. Una respuesta sin evidencia se guarda como en la entrega 1: `contenido` es la aclaración y `datos_respuesta` la estructura original completa.

### Fallos y recuperación

| Fallo | Resultado | Estado de la reserva |
|---|---|---|
| Recurso del RAG no disponible en una consulta nueva (esquema vectorial, `VECTOR_DATABASE_URL`, proveedores) | 503 / 502 con su código; se detecta antes de reservar | sin reserva |
| Falla la apertura de la sesión vectorial | 502 `VECTOR_DATABASE_NOT_CONFIGURED` u otro error controlado | `FALLIDO`; la misma clave se reintenta |
| Se agota el presupuesto global de 240 s | 504 `ASSISTANT_GENERATION_TIMEOUT`; ningún mensaje | `FALLIDO`; la misma clave se reintenta |
| Error o timeout del proveedor | 502 / 504 con el código de siempre | `FALLIDO`; la misma clave se reintenta |
| Fallo al persistir tras obtener la respuesta | 503 `CONVERSATION_PERSISTENCE_FAILED`; ningún mensaje, `actualizada_en` intacta | `FALLIDO` (o vence si ni siquiera se pudo marcar) |
| Proceso detenido | la conversación queda ocupada hasta el vencimiento; después se recupera | `EN_CURSO` → recuperada o `VENCIDO` |
| Ejecución tardía tras perder la reserva | 409 `CONVERSATION_QUERY_SUPERSEDED`; no publica | la de la ejecución vigente |
| Respuesta no persistible (contenido vacío o NUL) | **500 `ASSISTANT_RESPONSE_NOT_PERSISTABLE`**, específico y documentado en OpenAPI; ningún mensaje; no se trunca ni se altera la respuesta ni se relajan los CHECK | `FALLIDO` (sin estado terminal nuevo) |
| Respuesta sin evidencia | éxito normal (201) | `COMPLETADO` |

`ASSISTANT_RESPONSE_NOT_PERSISTABLE` **no es un fallo temporal** como los de proveedor (502/504) o de persistencia (503): reintentar la misma pregunta puede producir el mismo resultado, así que **el frontend no debe reintentarlo automáticamente en bucle** (puede ofrecer reformular la pregunta o informar al soporte). No se añadieron estados terminales, tablas ni migraciones para este caso.

No se registra un éxito antes de confirmar la persistencia. Los registros del servidor llevan solo identificadores, órdenes y conteos, nunca preguntas, respuestas ni citas. No se inventa una respuesta del asistente para representar un error técnico.

### Contrato para el frontend

**Petición.** Solo dos campos; cualquier otro (`usuario_id`, `ambiente`, `empresa_id`, `sector`, `anio`, `respuesta`, `citas`, `procedencia`, `orden`, `mensajes`…) se rechaza con 422.

```json
{
  "clave_idempotencia": "3f6c1b52-9a1e-4d3b-8c57-2b0e6a4d9f10",
  "pregunta": "¿Cuánto se redujo el consumo de agua?"
}
```

**Respuesta 201** (intercambio nuevo). 200 con `"reutilizado": true` si ya estaba completado.

```json
{
  "conversacion_id": "9c5a2b7e-1d4f-4a86-b3c0-5e7f8a9b0c12",
  "clave_idempotencia": "3f6c1b52-9a1e-4d3b-8c57-2b0e6a4d9f10",
  "pregunta": {
    "id": "b7d1c0a2-0001-4000-8000-000000000005",
    "orden": 5,
    "rol": "usuario",
    "contenido": "¿Cuánto se redujo el consumo de agua?",
    "respuesta": null,
    "procedencia": "ENVIADO_POR_USUARIO",
    "creado_en": "2026-11-07T16:20:11Z"
  },
  "respuesta": {
    "id": "b7d1c0a2-0002-4000-8000-000000000006",
    "orden": 6,
    "rol": "asistente",
    "contenido": "La empresa redujo su consumo de agua en 12 %. [1]",
    "respuesta": {
      "respuesta": "La empresa redujo su consumo de agua en 12 %. [1]",
      "afirmaciones": [
        {
          "texto": "La empresa redujo su consumo de agua en 12 %.",
          "referencias": [1],
          "cita_textual": "redujo el consumo de agua en 12 %"
        }
      ],
      "citas": [
        {
          "referencia": 1,
          "documento_id": "7e2f4c1a-6b0d-4e57-8c1f-2d9a5b3e7f40",
          "documento_nombre": "memoria-2025.md",
          "empresa_id": "0d3f0f2e-8d44-4d8e-9a5b-6a1b3f0f9c10",
          "empresa_nombre": "Minera Andina",
          "anio": 2025,
          "seccion": "Agua",
          "extracto": "En 2025 la empresa redujo el consumo de agua en 12 %..."
        }
      ],
      "sin_evidencia": false,
      "motivo": null,
      "mensaje": null,
      "afirmaciones_descartadas": 0
    },
    "procedencia": "GENERADO_POR_SERVIDOR",
    "creado_en": "2026-11-07T16:20:11Z"
  },
  "total_mensajes": 6,
  "actualizada_en": "2026-11-07T16:20:11Z",
  "reutilizado": false
}
```

**Error de operación en curso** (409, con cabecera `Retry-After: 5`):

```json
{
  "error": {
    "code": "CONVERSATION_QUERY_IN_PROGRESS",
    "message": "Esta consulta sigue en curso. Espere y reintente con la misma clave de idempotencia: no se inició otra ejecución.",
    "details": { "reintentar_en_segundos": 5 },
    "request_id": "8d0c6f52-7a31-4b1e-9f0a-1c2d3e4f5a6b"
  }
}
```

**Cómo usarlo**

1. Generar un UUID `clave_idempotencia` **por envío** de una pregunta y reutilizarlo en todos sus reintentos y dobles clics. Una pregunta nueva exige una clave nueva; la misma clave con otra pregunta da 409.
2. 201 y 200 son éxito; `reutilizado` indica si se creó o se recuperó. Añadir `pregunta` y `respuesta` a la vista usando su `orden`.
3. `CONVERSATION_QUERY_IN_PROGRESS`: esperar `Retry-After` y **reintentar con la misma clave**. `CONVERSATION_BUSY`: hay otra consulta en curso; deshabilitar el envío mientras tanto.
4. 502, 503 y 504 (incluido `ASSISTANT_GENERATION_TIMEOUT`): no se guardó nada; reintentar con la misma clave, con espera y un número acotado de intentos. `CONVERSATION_QUERY_SUPERSEDED`: reintentar con la misma clave. **500 `ASSISTANT_RESPONSE_NOT_PERSISTABLE`: no reintentar automáticamente.** Un reintento de algo ya guardado devuelve 200 aunque la parte vectorial o de IA esté caída.
5. Mostrar `procedencia` (`APORTADO_POR_CLIENTE` es lo importado; el resto lo generó o envió el servidor) y, si `contexto.empresa_activa` es falso, no ofrecer preguntas nuevas (se rechazan con 400 `COMPANY_INACTIVE`).
6. Con 40 mensajes la conversación solo se lee (`CONVERSATION_MESSAGE_LIMIT_REACHED`).
7. No enviar el historial ni esperar que el asistente entienda «y el año anterior»: cada pregunta debe ser autosuficiente.

### Archivos

Nuevos: `app/migraciones/transaccional/0010_continuacion_asistente.sql`, `app/modules/asistente/continuacion_router.py`, `continuacion_schemas.py`, `continuacion_service.py`, `tests/services/test_continuacion_asistente_postgres.py` y este informe. Modificados: `app/models/asistente.py`, `app/migraciones/catalogo.py` y `firmas.json` (solo `intercambios_asistente`), `scripts/crear_tablas.py`, `app/main.py`, `app/modules/asistente/domain/historial.py` (función `contiene_nul`), `app/modules/asistente/historial_schemas.py` (descripción de la procedencia), `pruebas-zap/alcance/operaciones.json`, `pruebas-zap/pruebas/fixtures/openapi-real.json`, dos pruebas Node de ZAP (16 excluidas) y las pruebas que fijaban el número de migraciones o de tablas (1–9 → 1–10).

Ajuste 2.1 (solo estos): `app/modules/asistente/continuacion_service.py` (`RecursosRag`, presupuesto global, orden de preparación), `continuacion_router.py` (`get_recursos_rag`, sin dependencias vectoriales, respuestas documentadas), `tests/services/test_continuacion_asistente_postgres.py` (15 pruebas nuevas y adaptación de los dobles), `tests/services/test_historial_asistente.py` (la ruta no depende de nada vectorial ni de IA), `pruebas-zap/pruebas/fixtures/openapi-real.json` (solo la operación de la continuación) y este informe. No cambian migraciones, modelo, firmas, `POST /asistente/consultas` ni las rutas de guardar, listar y recuperar.

### Pruebas (locales, PostgreSQL 17.4 aislado, proveedores y búsqueda vectorial como dobles)

- `test_continuacion_asistente_postgres.py`: **66 pasaron** (51 de la entrega 2 y 15 del ajuste 2.1), con PostgreSQL 17.4 aislado (una base nueva clonada de una plantilla migrada por prueba). Cubren, por número del encargo: (1–3) continuación con orden, citas y respuesta completa, procedencias, propietario, ambiente y contexto fijados por el servidor, y 11 campos que el cliente no puede enviar; (4) conversación ajena, inexistente o de otro ambiente con el mismo 404 y cero construcciones de proveedores, más 401 y 403; (5) empresa inactiva y sector cambiado; (6) respuesta sin evidencia (sin fragmentos y del modelo); (7–8) reintento completado sin llamadas ni revalidación, reintento de un intercambio anterior, y misma clave con otra pregunta; (9) misma clave simultánea forzada con barrera, midiendo que decidió `uq_intercambios_asistente_clave`; (10) consultas distintas simultáneas forzadas con barrera, decidió el índice parcial, y una segunda consulta mientras otra está en el LLM; (11) dos conversaciones dentro del LLM a la vez (barrera de dos partes: si se serializaran, expira); (12) sondeo desde otra conexión durante el embedding y el LLM: ninguna sesión «idle in transaction», `FOR UPDATE NOWAIT` sin esperas y reserva ya confirmada; (13) fallo de persistencia sin par parcial, proveedor no configurado, errores 502/504 con reintento por la misma clave y respuesta no persistible (NUL); (14) proceso detenido y recuperación, ejecutor tardío rechazado (tras recuperar la misma clave y tras ser sustituido por otra consulta) y reserva vencida sin recuperar; (15) 40 mensajes, último par que cabe, tamaño medido igual que el guardado inicial, y par que no cabe sin truncar; (16) reintento del guardado inicial tras continuar; (17) migración 0010 sobre datos de la entrega 1, instalación nueva y restricciones probadas con SQL directo; (18) `POST /asistente/consultas` sin guardar nada.
- Ajuste 2.1, en ese mismo archivo: reintento completado con esquema vectorial no preparado, configuración vectorial ausente o inválida y proveedores sin configurar (200, mismo intercambio, cero llamadas a esquema, URL, sesión o proveedores); estas pruebas usan la dependencia REAL `get_recursos_rag` y una app sin `app.state.ingesta`, es decir, **sin neutralizar** lo que quieren detectar; 404 de conversación ajena, inexistente o de otro ambiente antes de preparar nada, con lo vectorial ausente; consulta nueva con esquema o configuración no disponibles (503 / 502, sin reserva ni mensajes); cierre de la sesión vectorial en éxito, en fallo y ausencia de apertura en la repetición; fallo al abrir la sesión tras reservar (`FALLIDO`, recuperable); agotamiento del presupuesto global (504, sin mensajes, `FALLIDO`, sesión cerrada, reintento con la misma clave) con plazos de 0,3 s y eventos, sin esperas largas; presupuesto aplicado también a los embeddings y `TimeoutError` ajeno no reinterpretado; ejecutor desplazado que continúa pero no publica (409 `CONVERSATION_QUERY_SUPERSEDED`); y el error 500 no persistible, documentado en OpenAPI y distinto de 502/503/504.
- Comprobación de que las pruebas protegen: declarar de nuevo el esquema vectorial como dependencia de la ruta hace fallar las pruebas de reintento completado, de 404 y la de dependencias; se restauró el código. Antes, quitar la condición de token en la fase 3 hizo fallar las dos pruebas de ejecutor tardío.
- Historial completo tras el ajuste: `test_historial_asistente.py` (37), `test_historial_asistente_postgres.py` (35) y la continuación (66): **138 pasaron**. `test_asistente_router.py`, `test_asistente.py`, `test_asistente_postgres.py` y `test_sector_otros_asistente_postgres.py` (contrato y comportamiento de `POST /asistente/consultas`): **82 pasaron**. Antes del ajuste, la entrega 1 daba 72. Se ajustaron tres pruebas de la 0009 para que migren hasta la versión 9 y una comprobación de rutas para separar la continuación (que sí usa el RAG) de las rutas que no dependen de IA.
- Regresión relacionada de la entrega 2 original, ejecutada en lotes (todos con éxito; **no se repitió** tras el ajuste 2.1, que no toca migraciones, modelo ni `crear_tablas`): historial completo, 123; migraciones, esquema central, análisis GRI y auditoría con PostgreSQL, 68 y 9 omitidas; migraciones NOT NULL, reportes, sector «Otros», asistente (router, servicio y PostgreSQL) y cabeceras, 156 y 7 omitidas; motor de migraciones simulado, arranque, `crear_tablas`, SQL de operaciones y firmas, 75 y 1 omitida. Las omitidas son las de Docker/pgvector. Se actualizaron las aserciones que fijaban el número de migraciones (1–9 → 1–10), los conjuntos de tablas sin DDL automático y los recuentos de ZAP, sin relajar ninguna condición.
- ZAP: `unidad.test.mjs` pasó (32), también tras regenerar el fixture en el ajuste 2.1. **`ejecutor.test.mjs` y `permisos.test.mjs` NO se completaron:** el sistema detuvo las ejecuciones por falta de memoria (en una ejecución previa, 3 pasaron y 1 expiró a los 60 s). El cambio de recuento de `ejecutor.test.mjs` (16 excluidas) queda sin verificar. Tampoco hubo escaneo ZAP real.

### Limitaciones y pendientes

- **Una llamada exacta no se garantiza** (ver Idempotencia). Ante una caída posterior a la respuesta del proveedor, el reintento consulta de nuevo.
- Una ejecución cancelada o un proceso detenido dejan la reserva `EN_CURSO` hasta su vencimiento (hasta 300 s) y esa conversación rechaza otras consultas con `CONVERSATION_BUSY`.
- Las fases de reserva y publicación (transacciones cortas) no están bajo el presupuesto global y no tienen plazo propio: los 60 s de holgura entre el presupuesto y la reserva son un margen de diseño, no un timeout. Cancelar la espera del RAG no detiene necesariamente el trabajo externo ya iniciado.
- La sesión de la base **vectorial** conserva su lectura abierta durante la llamada al LLM, igual que en `POST /asistente/consultas` (no se cambió el RAG). No es una transacción del historial.
- No hay limpieza de reservas `FALLIDO`/`VENCIDO` antiguas. Un reintento con la misma clave **reutiliza su fila de reserva** (incrementa `intentos`): no se crea una fila nueva por cada intento fallido. Crece una fila por clave que no llegó a completarse y por reserva vencida sustituida por otra clave. Pendiente de decidir una política de retención.
- No se registra auditoría de las consultas, como `POST /asistente/consultas`.
- No ejecutado (Docker detenido): las pruebas con pgvector/PostgreSQL en contenedor (9 vectoriales de `test_migraciones.py`, 7 variantes `[contenedor]` de `test_migraciones_not_null.py`, `test_fragmentos_vectoriales.py` y las firmas vectoriales). La búsqueda vectorial real, OCI y DeepSeek no se probaron aquí; sí la lógica del RAG con dobles. Tampoco la regresión completa, Jenkins, Sonar, un escaneo ZAP real ni QA/UAT: la clasificación ZAP solo prueba el alcance y el contrato, y `ejecutor.test.mjs` no se completó por falta de memoria (ver Pruebas).
- **Orden de integración:** la 0009 y la 0010 dependen de la 0008 (sector `OTROS`), que sigue sin commitear en este árbol. Integrar en ese orden.
- El fixture `openapi-real.json` cambia dos esquemas existentes (`ProcedenciaMensaje`, `MensajeGuardado`) por los valores nuevos de procedencia y, en el ajuste 2.1, **solo** la operación de la continuación (sin parámetros vectoriales y con las respuestas 500/502/503/504 documentadas). Se generó con Python 3.12 normalizando el texto «Unprocessable Content» del fixture versionado.
- **Pendiente:** las suites Node `ejecutor.test.mjs` y `permisos.test.mjs` siguen sin completarse (el sistema detuvo la ejecución por falta de memoria); no se mezclaron con el ajuste. Tampoco se ejecutaron Docker/pgvector, Jenkins, Sonar, un escaneo ZAP real ni QA/UAT.
- Frontend: no se tocó.

### Para la entrega 3

Memoria conversacional (interpretar preguntas de seguimiento): decidir cuánto historial se envía al modelo, cómo se reformula y cómo se limita el contexto. Los mensajes `APORTADO_POR_CLIENTE` **no son contexto verificado** y no deben tratarse como tal. Decidir también la retención de reservas antiguas y, si se amplían los límites, la paginación de mensajes. La eliminación de conversaciones sigue fuera de alcance.

## B. Cambios necesarios en Análisis y Diseño

No se actualizó ningún documento: las fuentes (`igualab_md/01-Mockups-y-Propuestas`, LaTeX y Markdown, con numeraciones distintas) son externas al repositorio o están ignoradas por git. Texto propuesto, a continuación de lo propuesto en la entrega 1 (los números de RF y RN los asigna el equipo):

### B.1 Caso de uso (CU004)

- *Flujo Alternativo*, añadir: «**Continuar conversación.** El Administrador abre una conversación guardada y envía una pregunta nueva. El sistema responde con el asistente dentro de la empresa, el sector y el año de la conversación, y guarda automáticamente la pregunta y la respuesta completa, con sus citas, en el historial. Repetir el envío no duplica la conversación. Cada pregunta se responde de forma independiente: el asistente no usa las preguntas anteriores para interpretar la nueva.»
- *Post Condiciones*, añadir: «Éxito: la pregunta y la respuesta quedan guardadas juntas, con su orden, y la conversación se actualiza. Fallo: no se guarda ninguno de los dos mensajes y la conversación queda disponible; el usuario puede reintentar el envío.»
- *Restricciones*, añadir: «Recuperar una respuesta ya guardada no depende de la base vectorial ni de los proveedores de IA. La generación de una respuesta nueva tiene un tiempo máximo de 240 segundos; si se agota no se guarda nada y el envío puede repetirse. Si la respuesta generada no puede almacenarse (contenido vacío o con caracteres no admitidos), el sistema informa un error específico y no debe reintentarse automáticamente. Solo se procesa una consulta a la vez por conversación. Una conversación admite hasta 40 mensajes y 1 000 000 bytes de contenido; al alcanzarlos solo puede leerse. No se pueden hacer consultas nuevas sobre una empresa desactivada, aunque su historial siga consultable. Esta versión no usa el historial como memoria del asistente.»

### B.2 Requisitos y reglas

- RF nuevo: «Añadir una consulta a una conversación guardada del asistente y guardar automáticamente la pregunta y la respuesta completa con sus citas.»
- RN nueva «Contexto inmutable de la conversación»: «SI se continúa una conversación, ENTONCES la consulta usa la empresa, el sector y el año con que se guardó y no pueden cambiarse. SE APLICA A el historial del asistente. EXCEPCIÓN ninguna.»
- RN nueva «Una consulta en curso por conversación»: «SI una conversación tiene una consulta en curso, ENTONCES otra consulta distinta se rechaza con un conflicto explícito y no se encola. Conversaciones distintas se procesan a la vez.»
- RN nueva «Persistencia atómica del intercambio»: «SI una consulta se responde, ENTONCES pregunta y respuesta se publican juntas o no se publica ninguna, y los mensajes anteriores nunca se reescriben.»
- RN nueva «Idempotencia del envío»: «SI el envío se repite con la misma clave y la misma pregunta, ENTONCES se devuelve el intercambio ya guardado sin consultar de nuevo al asistente. La misma clave con otra pregunta es un conflicto. Se garantiza un intercambio guardado por clave, no una sola llamada al proveedor de IA ante caídas o pérdida de conexión.»
- Ampliar la RN «Procedencia del historial» de la entrega 1: «Los mensajes se marcan como aportados por el cliente (historial importado), enviados por el usuario (preguntas) o generados por el servidor (respuestas del asistente). La procedencia la fija siempre el servidor y la importada no se reclasifica.»
- RNF a documentar: presupuesto global de generación de 240 s (embeddings, búsqueda, chat y verificación), reserva de consulta de 300 s mayor que ese presupuesto, y recuperación tras caídas. Aclarar que cancelar una espera no garantiza detener el trabajo externo y que se asegura como máximo un intercambio guardado por clave, no una sola llamada al proveedor.
- RN nueva «Disponibilidad del historial»: «SI una respuesta ya fue guardada, ENTONCES puede recuperarse y repetirse el envío aunque los servicios de IA o la base vectorial no estén disponibles. SE APLICA A la continuación de conversaciones.»

### B.3 Modelo de datos

- Añadir `\tabname{intercambios\_asistente}` (reserva de consulta: `conversacion_id`, `clave_idempotencia`, `huella_pregunta`, `estado` ∈ {EN\_CURSO, COMPLETADO, FALLIDO, VENCIDO}, `token_ejecucion`, `vence_en`, `intentos`, `codigo_error`, `mensaje_pregunta_id`, `mensaje_respuesta_id`, fechas) con sus restricciones: `UNIQUE (conversacion_id, clave_idempotencia)`, índice único parcial `(conversacion_id) WHERE estado = 'EN_CURSO'`, `UNIQUE` en cada mensaje referenciado, y los CHECK de estado, huella, intentos, coherencia de mensajes y fechas.
- En `mensajes\_asistente`, la columna `procedencia` admite `APORTADO_POR_CLIENTE`, `ENVIADO_POR_USUARIO` y `GENERADO_POR_SERVIDOR`, ligada al rol por CHECK.
- **Recuento de tablas físicas: 19, 16 transaccionales y 3 vectoriales**, incluido el registro de migraciones de cada base (eran 18 con las dos tablas de la entrega 1). Transaccionales: `usuarios`, `sesiones`, `tokens_recuperacion`, `empresas`, `auditoria`, `reportes_prospeccion`, `documentos`, `operaciones_ingesta`, `analisis_gri_control`, `analisis_gri_estados`, `analisis_gri_historial`, `analisis_gri_validaciones`, `conversaciones_asistente`, `mensajes_asistente`, `intercambios_asistente`, `igualab_migraciones`. Vectoriales: `fragmentos_documento`, `cierres_documento`, `igualab_migraciones`. Las tablas `consultas_asistente` y `consulta_citas` del documento siguen sin existir en el código y no se crean.
- Trazabilidad: añadir los RF y RN nuevos a la fila del Asistente de IA (CU004).
