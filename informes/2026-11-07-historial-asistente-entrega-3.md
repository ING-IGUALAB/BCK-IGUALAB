# Historial del Asistente IA — Entrega 3: preguntas de seguimiento (con el cierre local)

Fecha: 2026-11-07/08. Estado: implementado y verificado **en local con dobles** de los proveedores, más **dos evaluaciones reales acotadas de la interpretación** (6 solicitudes y, tras un ajuste del prompt, 1 solicitud; 2026-11-08; ver abajo); sin staging, commit, push ni despliegue. La funcionalidad **no** se da por cerrada en ningún sistema desplegado. Sin migraciones ni tablas nuevas en esta entrega. Complementa los informes de la [entrega 1](2026-11-07-historial-asistente-entrega-1.md) y la [entrega 2 con su ajuste 2.1](2026-11-07-historial-asistente-entrega-2.md), que no se repiten aquí.

> **La comprensión conversacional real NO está demostrada.** Hay tres cosas distintas: (1) las pruebas con **dobles** (respuestas programadas), que demuestran el flujo y las validaciones, no la calidad del modelo; (2) **evaluaciones reales acotadas de la interpretación**: una primera pasada de 6 casos sintéticos (**5 cumplieron los criterios y 1 no**) y, tras ajustar el prompt, una repetición de ese único caso (**cumplió**); es una muestra muy pequeña; y (3) la **verificación del RAG completo con el modelo real** (recuperación, generación y citas), que **no se ejecutó**.

## A. Solución aplicada

### Qué hace

`POST /asistente/conversaciones/{conversacion_id}/consultas` interpreta la pregunta antes del RAG con **como máximo una llamada adicional** al LLM existente. El frontend sigue enviando solo `pregunta` y `clave_idempotencia`. `POST /asistente/consultas` no cambió (consulta independiente, sin memoria).

| | Responsabilidad | Quién |
|---|---|---|
| A | Interpretar la pregunta actual con el historial reciente y decidir `CONSULTAR` (con una pregunta autónoma), `ACLARAR` o `CAMBIAR_CONTEXTO` | `domain/interpretacion.py` + `continuacion_service._interpretar` |
| B | Responder: recuperación vectorial, prompt, verificación de afirmaciones y citas | `service.responder_consulta`, **sin cambios** |

**El historial ayuda a interpretar; no es evidencia.** La respuesta se recupera y se verifica de nuevo con los documentos elegibles actuales de la empresa y el año de la conversación. Ninguna cita ni respuesta anterior se reutiliza. Se guarda la pregunta **original**; la autónoma no se guarda ni se devuelve.

### Ejemplo completo

Conversación guardada de *Minera Andina* (MINERIA, 2025); su último intercambio fue «¿Qué sanciones tuvo la empresa en 2025?» → «La empresa registró dos sanciones ambientales. [1]». Petición (no se reenvía nada del historial):

```json
{
  "clave_idempotencia": "6a0d3c1e-52b7-4f1b-9c8e-1d2f3a4b5c6d",
  "pregunta": "¿Y cuál fue la de mayor importe?"
}
```

En el servidor: (1) se reserva la consulta; (2) se leen los últimos intercambios de **esa** conversación; (3) el LLM de interpretación devuelve `{"decision": "CONSULTAR", "pregunta_autonoma": "¿Cuál fue la sanción de mayor importe de la empresa en 2025?"}`; (4) la recuperación y la generación usan esa pregunta con los filtros de la conversación; (5) se guardan la pregunta original y la respuesta. Respuesta 201 (los documentos del ejemplo son ficticios y el pasaje recuperado contiene **ambos importes**, de modo que la comparación tiene base):

```json
{
  "conversacion_id": "9c5a2b7e-1d4f-4a86-b3c0-5e7f8a9b0c12",
  "clave_idempotencia": "6a0d3c1e-52b7-4f1b-9c8e-1d2f3a4b5c6d",
  "pregunta": {
    "id": "b7d1c0a2-0001-4000-8000-000000000003",
    "orden": 3,
    "rol": "usuario",
    "contenido": "¿Y cuál fue la de mayor importe?",
    "respuesta": null,
    "procedencia": "ENVIADO_POR_USUARIO",
    "creado_en": "2026-11-08T10:15:02Z"
  },
  "respuesta": {
    "id": "b7d1c0a2-0002-4000-8000-000000000004",
    "orden": 4,
    "rol": "asistente",
    "contenido": "Entre las dos multas que menciona el pasaje recuperado, la de mayor importe fue la de S/ 120 000 por emisiones atmosféricas. [1]",
    "respuesta": {
      "respuesta": "Entre las dos multas que menciona el pasaje recuperado, la de mayor importe fue la de S/ 120 000 por emisiones atmosféricas. [1]",
      "afirmaciones": [
        {
          "texto": "Entre las dos multas que menciona el pasaje recuperado, la de mayor importe fue la de S/ 120 000 por emisiones atmosféricas.",
          "referencias": [1],
          "cita_textual": "una multa de S/ 50 000 por vertimientos y otra multa de S/ 120 000 por emisiones atmosféricas"
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
          "seccion": "Sanciones",
          "extracto": "En 2025 se impuso una multa de S/ 50 000 por vertimientos y otra multa de S/ 120 000 por emisiones atmosféricas."
        }
      ],
      "sin_evidencia": false,
      "motivo": null,
      "mensaje": null,
      "afirmaciones_descartadas": 0
    },
    "procedencia": "GENERADO_POR_SERVIDOR",
    "creado_en": "2026-11-08T10:15:02Z"
  },
  "total_mensajes": 4,
  "actualizada_en": "2026-11-08T10:15:02Z",
  "reutilizado": false
}
```

La versión anterior de este ejemplo afirmaba «la sanción de mayor importe … fue la multa por vertimientos» con una cita que solo mencionaba esa multa, sin importes: **no respaldaba la comparación**. Se corrigió. Ese mismo escenario está ahora como prueba (ver abajo).

### Tres niveles de evidencia (que no son lo mismo)

| Nivel | Qué es | ¿Lo comprueba el sistema? |
|---|---|---|
| 1. Literalidad | la `cita_textual` aparece literalmente en un fragmento recuperado | **Sí**, `domain/citas.py` (referencias válidas, cita de al menos 20 caracteres y 3 palabras, y las cifras del texto constan en la cita o en su sección) |
| 2. Respaldo | el contenido citado respalda realmente la afirmación (implicación semántica) | **No.** Un pasaje real puede ser irrelevante o contradecir la afirmación; la literalidad no lo prueba. Lo revisa quien lee. Es una limitación **preexistente** del RAG, ajena al historial |
| 3. Exhaustividad | «la de mayor importe de **toda** la empresa» | **No.** El RAG recupera pocos fragmentos (k = 4): una comparación o un superlativo se limita a lo recuperado. El historial no cambia esto |

Lo que sí hace el verificador y se probó: una afirmación comparativa con cifras que no constan en la cita se descarta (`AFIRMACIONES_NO_VERIFICABLES`); una cita inexistente se descarta; y un antecedente histórico que el modelo repite sin respaldo actual no se vuelve verdadero. Lo que **no** hace, y también se probó para dejarlo explícito: una afirmación **sin cifras** con una cita literal pero irrelevante supera la verificación (por ejemplo «la de mayor importe fue la multa por vertimientos», citando un pasaje sin importes). No se implementó ningún motor de razonamiento, agregación de sanciones, verificación semántica adicional ni búsqueda exhaustiva.

### Selección del contexto

| Regla | Valor |
|---|---|
| Fuente | solo los mensajes de la conversación ya autorizada, leídos **después de reservar** (la ejecución dueña de la reserva es la única que puede añadir mensajes) |
| Ventana | los **últimos 4 intercambios completos**, en orden cronológico, y **12 000 caracteres** de representación (roles y procedencias incluidos) |
| Integridad | un intercambio entra completo o no entra; si no cabe se omite, el contexto queda marcado como limitado y **la ventana se detiene ahí** (no se saltan intercambios para presentar otros como los inmediatamente anteriores) |
| Contenido | pregunta, contenido visible de la respuesta, rol, procedencia y si fue sin evidencia; no `datos_respuesta` completos ni extractos históricos |
| Persistencia | ninguna adicional: sin resúmenes, embeddings del historial, tablas ni migraciones. No son límites de almacenamiento (siguen 40 mensajes y 1 000 000 bytes) |

Los diagnósticos registran solo identificadores, órdenes, tamaños y decisión; nunca preguntas, respuestas ni citas.

### Procedencia y seguridad del contexto

Los mensajes anteriores son **datos**: viajan dentro de **un solo mensaje de usuario**, como JSON entre marcadores con código aleatorio por petición, separados del contexto fijo y de la pregunta actual (una cadena JSON); jamás como `system` ni `developer`. `APORTADO_POR_CLIENTE` puede ayudar a identificar el tema pero no prueba que el servidor lo generó; `GENERADO_POR_SERVIDOR` tampoco es evidencia vigente; no se reclasifica nada. La decisión del modelo **nunca controla** la autorización ni la búsqueda: empresa, sector, año y ambiente salen de la conversación y del servidor. No se ejecuta nada que produzca el modelo; su salida se valida con un esquema estricto (sin campos extra ni coerciones). Estas medidas **reducen** el riesgo de manipulación del modelo; **no son una garantía absoluta**: lo verificable son las defensas posteriores (esquema estricto, filtros fijos y verificación de citas).

### Aclaraciones, cambio de contexto y errores

| Situación | HTTP y código | Efecto |
|---|---|---|
| Referencia no determinable | **400** `CONVERSATION_CLARIFICATION_REQUIRED` (`details.accion = REFORMULAR_CON_CLAVE_NUEVA`) | No se busca nada; ningún mensaje; reserva `FALLIDO` por token |
| Requiere otra empresa, sector o **año** | **400** `CONVERSATION_CONTEXT_CHANGE_REQUIRED` (`details.accion = INICIAR_CONVERSACION_NUEVA`) | Ídem; no se responde sobre el año actual ignorando lo pedido |
| Salida de interpretación inválida | **502** `CONVERSATION_INTERPRETATION_INVALID` | No se busca con una pregunta inventada; reintentable con la misma clave |
| Fallo del proveedor en la interpretación | 502 `LLM_PROVIDER_ERROR` (u otro código existente) | Reserva `FALLIDO`; reintentable con la misma clave |
| Presupuesto agotado | 504 `ASSISTANT_GENERATION_TIMEOUT` | Un **único** plazo de 240 s cubre contexto, interpretación y RAG |

El mensaje de los 400 es un texto fijo del servidor. Tras una aclaración o un cambio de contexto el usuario envía de nuevo **con una clave nueva**. Preguntar por **otro año** requiere otra conversación; en cambio una comparación que el documento del año actual ya incluye («¿cómo se compara con 2024 según ese informe?») *puede* mantenerse en el contexto actual: esa distinción la decide el modelo y es **justo lo que la evaluación real debe revisar** (caso opcional del conjunto).

### Integración con las entregas 2 y 2.1

Se conserva: autorización y 404 uniforme; reintento completado antes de preparar recursos (un reintento **no** interpreta, no lee contexto y no usa proveedores ni recursos vectoriales); una consulta en curso por conversación; sin transacciones ni bloqueos del historial durante las llamadas externas; token de propiedad y rechazo del ejecutor desplazado; par de mensajes atómico; procedencias; huella y clave del guardado inicial; límites de 40 mensajes y 1 000 000 bytes (se comprueban antes de interpretar). La pregunta original da la huella de idempotencia. Una ejecución recuperada vuelve a leer el contexto confirmado al empezar ese intento; **no se promete** que una generación fallida y su reintento den la misma respuesta.

### Contrato para el frontend

1. **Nuevos 400** para mostrar `error.message` y actuar según `error.details.accion`: `CONVERSATION_CLARIFICATION_REQUIRED` (pedir que reformule y **generar una clave nueva**) y `CONVERSATION_CONTEXT_CHANGE_REQUIRED` (ofrecer «iniciar una conversación nueva» con otra empresa, sector o año). No reintentar con la misma clave en un bucle.
2. **Nuevo 502** `CONVERSATION_INTERPRETATION_INVALID`: reintentar con la misma clave, con espera y número acotado de intentos.
3. El 504 de 240 s incluye la interpretación.
4. La pregunta puede ser un seguimiento; el frontend sigue sin enviar historial, contexto ni respuestas. Cambiar de empresa, sector o año **exige una conversación nueva**.
5. Las respuestas siguen siendo las de siempre. No presentar una comparación como «la mayor de toda la empresa»: se limita a lo recuperado y su cita debe revisarse.

```json
{
  "error": {
    "code": "CONVERSATION_CLARIFICATION_REQUIRED",
    "message": "No se pudo determinar a qué se refiere la pregunta con la conversación disponible. Reformúlela indicando el tema o la información que necesita y envíela de nuevo con una clave de idempotencia nueva.",
    "details": { "accion": "REFORMULAR_CON_CLAVE_NUEVA" },
    "request_id": "8d0c6f52-7a31-4b1e-9f0a-1c2d3e4f5a6b"
  }
}
```

### Cierre local: revisión final

Se revisó el código y las pruebas vigentes (no solo los resúmenes) contra los 14 puntos pedidos.

| # | Punto | Resultado | Dónde se comprueba |
|---|---|---|---|
| A | Autenticación y autorización antes de leer contexto o preparar recursos | Correcto: `AdministradorUser` es la primera dependencia; el contexto se lee tras reservar | `test_sin_sesion_401_y_superadmin_403…`, `test_una_conversacion_ajena_no_permite_leer_contexto_ni_preparar_proveedores` |
| B | Conversaciones limitadas a propietario y ambiente | Correcto: `_conversacion_propia` filtra por ambos; el mismo 404 | `test_una_conversacion_ajena_inexistente_o_de_otro_ambiente…` |
| C | Reintento completado sin interpretación, proveedores ni recursos vectoriales | Correcto: se resuelve antes de `preparar()` | `test_un_reintento_completado_no_prepara_ni_usa_recursos…` (4 situaciones), `test_un_reintento_completado_no_interpreta…` |
| D | Últimos 4 intercambios, 12 000 caracteres, sin saltar recientes | Correcto | pruebas de ventana (unitarias y con PostgreSQL) |
| E | Lectura transaccional cerrada antes de llamar al modelo | Correcto (`_mensajes_de_contexto` hace `rollback`). **Faltaba una prueba que lo demostrara para la interpretación**: se añadió | `test_la_lectura_del_contexto_se_cierra_antes_de_llamar_al_modelo_de_interpretacion` |
| F | Historial como datos con roles y procedencias | Correcto | pruebas de prompt y de instrucciones incrustadas |
| G | Pregunta original para idempotencia y persistencia | Correcto | `test_se_persiste_la_pregunta_original…` |
| H | Pregunta autónoma para recuperación y generación | Correcto (mutación comprobada) | `test_un_seguimiento_se_transforma…` |
| I | Empresa, sector, año y ambiente solo del servidor | Correcto | `test_la_decision_del_modelo_nunca_controla_los_filtros` |
| J | Presupuesto único de 240 s | Correcto | `test_el_plazo_global_cubre_la_interpretacion_y_el_rag_juntos` |
| K | Token, ejecutor desplazado, par atómico | Correcto | pruebas de ejecutor desplazado y de fallo de persistencia |
| L | 40 mensajes y 1 000 000 bytes | Correcto | pruebas de límites |
| M | Aclaración y cambio de contexto sin mensajes y con reserva liberada por token | Correcto | pruebas de aclaración y de cambio de contexto |
| N | `POST /asistente/consultas` independiente | Correcto | `test_post_consultas_sigue_siendo_independiente…` y su suite de 82 pruebas |

**Defectos funcionales demostrados: ninguno.** No se cambió código de producción en este cierre. Sí se corrigió **documentación** (el ejemplo comparativo y la redacción de B) y se añadieron pruebas (cierre de la lectura antes de la interpretación, evidencia en tres niveles, comprobación de antecedentes y arnés de evaluación). Un hallazgo que **no** es del historial: la limitación del nivel 2 de evidencia descrita arriba (preexistente en el RAG). Un caso límite no cubierto: si el presupuesto cancela una consulta a la base mientras se lee el contexto, `_marcar_fallido` hace `rollback` y reintenta marcar; las pruebas de presupuesto cancelan en los proveedores, no en una sentencia SQL en curso (no hay evidencia de un fallo, pero no está probado).

### Evaluación real de la interpretación (primera ejecución: 6 llamadas, 2026-11-08; resultado histórico)

**Alcance y límites autorizados.** Una sola pasada de los **6 casos núcleo** del conjunto sintético (empresa ficticia «Minera Andina»; ningún dato real, documento privado ni conversación de QA), con el modelo real **solo para la interpretación**: sin embeddings, sin recuperación vectorial y sin la generación final del RAG. Máximo 6 solicitudes, una por caso, sin reintentos, séptimo caso excluido, 30 s por llamada y salida acotada a 800 tokens.

| Dato | Valor |
|---|---|
| Proveedor y endpoint efectivos (configuración del proyecto) | DeepSeek, `https://api.deepseek.com` |
| Modelo efectivo (`DEEPSEEK_MODEL`) | `deepseek-flash` (coincide con el valor por defecto; se leyó de la configuración, no del informe) |
| Fecha de ejecución | 2026-11-08 |
| **Solicitudes reales al proveedor** | **6** (una por caso, ningún error ni reintento; duración total de la corrida 25,4 s) |
| Séptimo caso | no ejecutado |
| Ajuste al arnés previo a la ejecución (solo pruebas) | un adaptador de prueba con el mismo cuerpo que producción (mismo modelo, endpoint y temperatura 0,2) que añade `max_tokens: 800` y conserva el estado HTTP del error; el adaptador de producción no se tocó. Se añadió además la interrupción de la pasada ante un error de configuración (HTTP 400/401/403/404) para no gastar llamadas: no se activó |

**Resultados** (decisión y criterios automáticos; la columna «evaluación razonada» es mi revisión, que **requiere confirmación humana**):

| # | Caso (objetivo) | Esperada → obtenida | Pregunta autónoma devuelta | Validación automática | Duración | Evaluación razonada |
|---|---|---|---|---|---|---|
| 1 | `referencia-clara` (resolver «la de» sin cambiar la intención) | CONSULTAR → **CONSULTAR** | «De las sanciones de Minera Andina en 2025, ¿cuál fue la de mayor importe?» | **Cumple** | 3,19 s | Correcto: resuelve el referente (sanciones), conserva la intención («mayor importe») y no añade importes ni cantidades. Añade empresa y año, que provienen del contexto fijo de la conversación (no son hechos nuevos) |
| 2 | `referencia-ambigua` (pedir aclaración si «eso» puede ser agua o emisiones) | ACLARAR → **ACLARAR** | — | **Cumple** | 2,94 s | Correcto |
| 3 | `cambio-de-tema` (respetar el tema nuevo) | CONSULTAR → **CONSULTAR** | «¿Cuál es el consumo de energía de Minera Andina en 2025?» | **Cumple** | 4,97 s | Correcto: no arrastra las sanciones. Igual que en el 1, añade empresa y año del contexto fijo |
| 4 | `otro-anio-requiere-otra-conversacion` («¿Y el año anterior?») | CAMBIAR_CONTEXTO → **CAMBIAR_CONTEXTO** | — | **Cumple** | 2,81 s | Correcto: distingue pedir otro año. Solo prueba esta mitad del criterio 4; la otra (comparación contenida en el documento actual) es el séptimo caso, **no ejecutado** |
| 5 | `respuesta-previa-sin-evidencia` (no tratar como hecho una respuesta sin evidencia) | ACLARAR → **CONSULTAR** | «¿Cuál fue la sanción de mayor importe que tuvo Minera Andina en 2025?» | **No cumple** (decisión distinta de la esperada) | 3,50 s | Discutible, ver abajo |
| 6 | `antecedente-importado-incorrecto` (verificar «S/ 50 000» sin darlo por cierto) | CONSULTAR → **CONSULTAR** | «¿Es correcto que la multa de Minera Andina en 2025 fue de S/ 50 000?» | **Cumple** | 4,94 s | Correcto: conserva el carácter de comprobación; la cifra aparece como lo que el usuario pide verificar, no como premisa. Añade empresa y año del contexto fijo |

**Aprobados 5, fallidos 1, no ejecutados 1 (el séptimo).** Sin errores del proveedor, sin salidas inválidas y sin timeouts.

**Caso 5 (el único fallo), clasificado.** No es un fallo del proveedor ni una salida inválida: es una **interpretación discutible con un criterio esperado ambiguo**.
- *Qué pasó:* la respuesta anterior decía que no se encontró evidencia de sanciones. El modelo entendió «la de» como «la sanción» (el referente existe en la pregunta anterior del usuario) y devolvió una consulta nueva. No dio ninguna cifra ni hecho por demostrado.
- *Por qué es discutible:* «¿Cuál fue la sanción de mayor importe…?» **presupone que existen sanciones**, cuando el último intercambio no pudo establecerlo. El caso esperaba ACLARAR, pero su propia nota de revisión admite que CONSULTAR sería defendible si la pregunta no presupone su existencia. El criterio no distingue esas dos lecturas, y no se cambia ahora que se vio la salida.
- *Efecto real si se despliega así:* el RAG volvería a buscar con los documentos actuales; si no hay sanciones, responde «sin evidencia» (no inventa). El riesgo es de experiencia (una pregunta que presupone), no de integridad de datos.
- *Causa probable:* el prompt de interpretación dice que el historial no es evidencia, pero no dice qué hacer cuando la respuesta anterior fue «sin evidencia» (el campo `sin_evidencia` sí se envía).
- *Corrección mínima propuesta (en el momento de esta ejecución no estaba aplicada; se aplicó después, ver la sección siguiente):* añadir una regla al prompt de interpretación: «si la respuesta anterior fue sin evidencia, no presupongas que existe lo preguntado: formula la pregunta sin presuponerlo o pide aclaración»; y, en la próxima evaluación, decidir de antemano si CONSULTAR sin presuposición cuenta como aceptable. En esa primera ejecución no se modificó ni el prompt ni el código de producción: los 6 casos corrieron con el prompt anterior a la regla 9.

**Observación común a los casos 1, 3 y 6 (para revisión humana).** El modelo añadió «de Minera Andina en 2025» a la pregunta autónoma. Procede del contexto fijo que el servidor envía y coincide con los filtros reales de la búsqueda, por lo que no cambia el alcance ni introduce hechos; pero amplía el texto de la pregunta con entidades que el usuario no escribió. Conviene que una persona confirme que es aceptable para el RAG.

**Conclusión sobre esta muestra.** En 6 casos sintéticos y una sola pasada, el modelo resolvió bien el referente claro, pidió aclaración en la ambigüedad, respetó el cambio de tema, detectó la petición de otro año y mantuvo la comprobación del antecedente importado; falló (o quedó discutible) donde la respuesta anterior carecía de evidencia. **Esto no demuestra comprensión universal, ausencia de alucinaciones, respaldo semántico de las respuestas ni resistencia general a instrucciones maliciosas** (ningún caso incluía instrucciones incrustadas), ni evalúa el caso límite de la comparación dentro del documento actual (séptimo caso pendiente), ni la variabilidad entre ejecuciones (una sola pasada; el modelo no es determinista). Tampoco se verificó el RAG completo con el modelo real.

**Seguimiento:** el criterio del caso 5 y la corrección propuesta se resolvieron después (sección siguiente). Quedan pendientes el séptimo caso y los casos con instrucciones incrustadas; cualquier evaluación nueva requiere una autorización nueva.

### Ajuste «respuesta anterior sin evidencia» y segunda evaluación real (1 solicitud, 2026-11-08)

**Regla funcional acordada.** Una respuesta anterior «sin evidencia» **no obliga siempre a ACLARAR** (ni siempre a CONSULTAR):
- *Tema o referente identificable:* puede devolver `CONSULTAR`, pero la pregunta autónoma **no debe convertir en hecho lo que la respuesta anterior no confirmó**; debe conservar la intención y expresar la condición sin inventar hechos (por ejemplo, «¿Hay evidencia de sanciones de Minera Andina en 2025 y, si la hay, cuál tiene el mayor importe?»; la redacción no es literal).
- *Referente no identificable:* `ACLARAR`; no se adivina a qué se refiere «eso» o «la de».
- La empresa y el año pueden incorporarse desde el contexto fijo, siempre que coincidan con los filtros de la conversación.

**Corrección aplicada (mínima).** Una regla nueva (la 9) en `SISTEMA_INTERPRETACION` (`domain/interpretacion.py`): cuando el historial trae `sin_evidencia: true`, esa respuesta no confirmó que existan los hechos preguntados y no se presupone su existencia; con tema identificable → `CONSULTAR` con pregunta autónoma **condicional**; con referente no identificable → `ACLARAR`; solo la empresa y el año del contexto fijo pueden incorporarse. **No** se añadió ninguna regla en el servidor que convierta automáticamente todo `sin_evidencia` en una de las dos salidas. No cambiaron el RAG, sus filtros, la verificación de citas, los mensajes históricos, el esquema, tablas, migraciones, límites, reservas ni idempotencia.

**Criterio del caso `respuesta-previa-sin-evidencia` (actualizado).** Decisión `CONSULTAR`; la pregunta autónoma debe conservar la intención (contener «sanci» y «mayor importe»), incluir una marca de condición (`evidencia`, `hay `, `existe`, `hubo`…) y no contener expresiones que den los hechos por establecidos («dado que», «puesto que», «ya que»…). ACLARAR no cumple *en este caso* porque el tema sí es identificable (ACLARAR queda cubierto por `referencia-ambigua`). Estos criterios son **mínimos y no bastan**: no se da por aprobado un caso por la validez del JSON ni por la presencia de la palabra «si»; el significado completo lo revisa una persona (`revision_humana`), y una prueba documenta un texto que presupone los hechos y aun así pasa el filtro léxico. La nota `nota_criterio` del conjunto conserva el criterio y el resultado anteriores.

**Resultado original de la primera pasada (conservado, no reclasificado):** 6 llamadas; 5 casos cumplieron y 1 no cumplió **el criterio de esa ejecución** (se esperaba ACLARAR y el modelo devolvió «¿Cuál fue la sanción de mayor importe que tuvo Minera Andina en 2025?»).

**Pruebas locales sin costo de este ajuste** (dobles; una corrida de las dos suites): **85 pasaron, 6 omitidas, 0 fallos**: `test_interpretacion_asistente.py` (49) y `test_seguimiento_asistente_postgres.py` (36). Antes de esa corrida, una ejecución previa de la primera suite tuvo 1 fallo en una aserción mía (una mayúscula inicial) que corregí; no hubo fallos de la regla. Cubren: el texto de la regla (distingue ambos casos y no fuerza ninguna salida), tema identificable tras una respuesta sin evidencia (el RAG recibe la consulta condicional, se persiste la pregunta original, los filtros no cambian), referente ambiguo que requiere aclaración sin buscar, la consulta condicional con un RAG sin documentos (ausencia de evidencia, nada inventado), la comprobación de un antecedente importado incorrecto (prueba existente: no se vuelve verdadero), empresa y año tomados del contexto fijo del servidor, y el esquema estricto (un `anio` o `sector` en la salida del modelo se rechaza). Las pruebas con dobles verifican el flujo y el contrato; **no demuestran que el modelo real obedezca la regla**.

**Única solicitud real autorizada** (selección comprobada antes de ejecutar: un solo caso, `respuesta-previa-sin-evidencia`; el resto no se ejecutó):

| Dato | Valor |
|---|---|
| Proveedor, endpoint y modelo efectivos | DeepSeek, `https://api.deepseek.com`, `deepseek-flash` (leídos de la configuración; temperatura 0,2 como en producción; 30 s máximo; salida acotada a 800 tokens) |
| Solicitudes reales de esta ejecución | **1** (sin error, sin reintento, 2,91 s). Total de solicitudes reales de la evaluación: **7** (6 + 1) |
| Embeddings, recuperación vectorial, RAG final | no se usaron |
| Decisión | `CONSULTAR` (esperada: `CONSULTAR`) |
| Pregunta autónoma completa | «¿Hay evidencia de sanciones de Minera Andina en 2025 y, si la hay, cuál es la de mayor importe?» |
| Validación automática | **Cumple** (sin incumplimientos) |

**Revisión razonada.** Conserva la intención (el mayor importe de las sanciones) y expresa la condición de forma explícita: pregunta primero si hay evidencia y solo «si la hay» pide la de mayor importe, de modo que **no presupone** que hubo sanciones. La empresa y el año provienen del contexto fijo y coinciden con los filtros de la conversación; no añade cifras ni conclusiones. Requiere confirmación humana.

**Limitaciones de este resultado.** Es **una** solicitud sobre **un** caso. La regla 9 incluye un ejemplo de redacción casi igual a lo que el modelo devolvió, así que este resultado muestra que el modelo puede **seguir ese ejemplo**, no que generalice la regla a otros temas, empresas o redacciones. Tampoco prueba comprensión universal, la ausencia de alucinaciones, el respaldo semántico de las respuestas, la resistencia a instrucciones incrustadas ni el comportamiento del RAG completo; no se ejecutaron los otros seis casos con la regla nueva (podría haber efectos en ellos que no se midieron) y el modelo no es determinista. Siguen pendientes la verificación del RAG completo con el modelo real, el frontend y el despliegue.

### Pruebas ejecutadas

Las pruebas de esta sección usan **dobles** (PostgreSQL 17.4 aislado; LLM, embeddings y búsqueda programados), salvo que se diga lo contrario. Las únicas corridas con el modelo real son dos, ambas solo de interpretación: la primera ejecución de 6 llamadas («Evaluación real de la interpretación») y, tras el ajuste del prompt, una repetición de 1 llamada sobre el caso afectado; **7 llamadas reales en total, en dos ejecuciones distintas**. El resultado de la primera no se reescribe como si hubiera corrido con la regla nueva, y no hubo una ejecución de los seis casos con el prompt final. Cada línea es **una corrida independiente**; no se suman entre sí.

**Ejecutadas en este cierre (tras los cambios de pruebas y de documentación):**

| Corrida | Resultado |
|---|---|
| `test_interpretacion_asistente.py` + `test_seguimiento_asistente_postgres.py` (una sola corrida) | **68 pasaron, 6 omitidas, 0 fallos**: 37 + 31 pasaron; las 6 omitidas son la evaluación con el modelo real (pendiente) |
| Pruebas nuevas del cierre dentro de esa corrida | cierre de la lectura antes de la interpretación (1); comparación respaldada por un pasaje con importes, cita literal sin importes, literalidad sin respaldo semántico y comprobación de un antecedente en 2 variantes (4 + 1 = 5 casos); criterios de contenido y sintéticos del conjunto de evaluación (2) |
| ZAP Node `ejecutor.test.mjs` (solo, secuencial) | **31 pasaron, 0 fallos, 0 canceladas** (564 s) |
| ZAP Node `permisos.test.mjs` (solo, secuencial) | **11 pasaron, 3 omitidas, 0 fallos**. Las omitidas piden Linux con un usuario no root (2) y Docker (1) |

**Causa de las expiraciones Node anteriores (identificada, no atribuida a la memoria):** las pruebas de `ejecutor.test.mjs` tardan de 16 a 25 s cada una (el archivo completo, unos 9 a 10 minutos). El primer intento sin tope no terminó en minutos y lo detuve; el segundo lo corté yo con `--test-timeout=60000`, que cancelaba una prueba; el tercero con un `timeout 550` externo, que mató el archivo cuando llevaba 29 pruebas pasadas. En esta corrida, secuencial y sin límites artificiales, terminó. Las ejecuciones que el sistema detuvo por falta de memoria fueron, en cambio, las de una sesión anterior que corrían junto con otras pruebas. No se aumentó ningún timeout ni se relajó ninguna aserción.

**Ejecutadas antes del cierre y no repetidas** (el código de producción no cambió después de ellas; son corridas independientes, no se suman a las anteriores): continuación (66), historial entrega 1 (37 + 35) y contrato de `POST /asistente/consultas` (82), junto con las cinco suites de la entrega 3 en una corrida conjunta de **197 pasaron y 6 omitidas**; `unidad.test.mjs` (32); y los lotes de migraciones y regresión de la entrega 2.

**No ejecutadas:**
- La verificación del **RAG completo** con el modelo real (embeddings, recuperación, generación y citas) y el séptimo caso de evaluación.
- Docker/pgvector (16 pruebas): el motor de Docker no responde (`dockerDesktopLinuxEngine` no existe); no se intentó levantarlo.
- Regresión completa, Jenkins, Sonar, escaneo ZAP real, llamadas reales de embeddings o RAG, y QA/UAT/producción.

**Omitidas por diseño** en las corridas sin la variable de evaluación: 6 (evaluación real) y 3 de `permisos.test.mjs` (requisitos de entorno). La corrida real con `IGUALAB_EVALUAR_MODELO_REAL=1` ejecutó solo esos 6 y dio **5 pasaron, 1 falló** (el caso 5), sin tocar otras pruebas. Esa fue la primera ejecución (6 llamadas); la repetición posterior del caso 5 (1 llamada, con la regla 9) cumplió el criterio actualizado. No se ejecutaron los seis casos con el prompt final.

### Limitaciones del modelo y del RAG

- **Interpretación (modelo):** la primera ejecución real (6 casos, 6 llamadas) dio 5 cumplimientos y 1 interpretación discutible (respuesta anterior sin evidencia); tras el ajuste del prompt, una llamada adicional sobre ese caso cumplió (7 llamadas reales en dos ejecuciones; los otros cinco casos no se repitieron con el prompt final); no cubre instrucciones incrustadas, el cambio de empresa, la variabilidad entre ejecuciones ni la comparación dentro del documento actual. «¿Y el año anterior?» depende por completo del modelo; no hay comprobación determinista del año (bloquearía comparaciones legítimas). Cada intento nuevo añade una llamada al LLM; un reintento tras fallo vuelve a interpretar y puede decidir otra cosa; si el último intercambio supera 12 000 caracteres el contexto queda vacío y los seguimientos pedirán aclaración. El usuario no ve cómo se interpretó su pregunta (no se guarda la autónoma).
- **RAG (preexistente, no introducido por el historial):** la verificación es léxica (nivel 1); no prueba implicación (nivel 2) ni exhaustividad (nivel 3). Recupera pocos fragmentos, así que los superlativos y comparaciones se limitan a ellos.
- **Se mantienen** las de las entregas 2 y 2.1: no se garantiza exactamente una llamada al proveedor (cancelar una espera no detiene el trabajo externo), reserva y publicación sin plazo propio (los 60 s entre el presupuesto y la reserva son un margen de diseño, no un timeout), reservas sin limpieza (un reintento reutiliza su fila) y sin auditoría de consultas.

### Inventario para la integración (verificado con `git status` y `git diff`)

No se hizo staging ni commit. HEAD `9541aef`, rama `development`, índice vacío. Orden necesario: **primero el sector OTROS (0008), después el historial (0009, 0010)**.

| Grupo | Archivos |
|---|---|
| **Sector OTROS** | `app/models/empresas.py`, `app/migraciones/compatibilidad.py`, `app/migraciones/transaccional/0008_sector_otros.sql`, `tests/routers/test_empresas.py`, `tests/services/test_ingesta_validacion.py`, `tests/services/test_sector_otros_{postgres,asistente_postgres,flujo_postgres,vectorial}.py`, `informes/2026-11-05-sector-otros.md` |
| **Historial y continuación (propios)** | `app/main.py`, `scripts/crear_tablas.py`, `app/models/asistente.py`, `app/modules/asistente/{historial_router,historial_schemas,historial_service,continuacion_router,continuacion_schemas,continuacion_service}.py`, `app/modules/asistente/domain/{historial,interpretacion}.py`, `app/migraciones/transaccional/{0009_historial_asistente,0010_continuacion_asistente}.sql`, `app/migraciones/firmas.json`, `pruebas-zap/alcance/operaciones.json`, `tests/services/test_{historial_asistente,historial_asistente_postgres,continuacion_asistente_postgres,seguimiento_asistente_postgres,interpretacion_asistente}.py`, `tests/services/datos/ejemplos_evaluacion_interpretacion.json`, `informes/2026-11-07-historial-asistente-entrega-{1,2,3}.md` |
| **Mezclados (OTROS + historial)** | `app/migraciones/catalogo.py` (entrada 8 de OTROS; entradas 9 y 10 del historial), `pruebas-zap/pruebas/fixtures/openapi-real.json` (el enum `OTROS` y las rutas y esquemas del historial), y las listas de versiones y recuentos de migraciones: `tests/services/test_{analisis_gri_migracion,auditoria_postgres,esquema_central_postgres,migraciones,migraciones_not_null,reportes_integridad_postgres,reportes_migracion_postgres}.py`, `tests/test_motor_migraciones_sin_base.py`, `tests/test_analisis_gri_arranque.py` (hunks de OTROS: 7→8; del historial: 8→10). Además `pruebas-zap/scripts/pruebas/{unidad,ejecutor}.test.mjs` y los conjuntos de tablas de `tests/services/test_{crear_tablas_plazos,operaciones_ingesta_sql}.py` son del historial |
| **Dependencia entre grupos** | `tests/ayudantes_analisis_gri.py` (parámetro `sector` de `crear_empresa`) es de OTROS pero **lo usan las pruebas del historial**: integrar OTROS antes |
| **Cambios ajenos (no tocados)** | `Jenkinsfile`, `app/core/security.py`, `scripts/limpiar_datos_demo_qa.py`, `task-2.md`, `tests/ayudantes_minio.py`, `tests/services/test_limpiar_datos_demo_qa.py`, `tests/services/test_limpiar_datos_demo_qa_minio.py` |

Los archivos mezclados contienen fragmentos de los dos grupos: separarlos exige elegir hunks (`git add -p`); no se preparó nada.

### Pendientes de integración

- Frontend (no se tocó): nuevos errores 400/502, nueva clave tras aclaración y la regla de no tratar comparaciones como exhaustivas.
- Despliegue: aplicar las migraciones 0008, 0009 y 0010 en QA/UAT no se hizo; nada se probó contra QA, UAT ni producción.
- Evaluación real: primera ejecución de 6 llamadas (5 cumplieron, 1 no) y una llamada adicional tras el ajuste, sobre el caso afectado, que cumplió: 7 llamadas reales en dos ejecuciones; el ajuste de la regla de respuestas sin evidencia ya está aplicado. Siguen pendientes el séptimo caso del conjunto, una evaluación más amplia con el prompt final y el RAG completo con proveedores reales. No ejecutado: verificación del RAG completo con el modelo real, Docker/pgvector (16 pruebas; Docker no responde), Jenkins, Sonar, escaneo ZAP real, regresión completa y llamadas reales de embeddings o RAG. Las suites Node `ejecutor` y `permisos` sí se ejecutaron (ver Pruebas).

## B. Cambios necesarios en Análisis y Diseño

No se actualizó ningún documento: las fuentes (`igualab_md/01-Mockups-y-Propuestas`, LaTeX y Markdown) son externas al repositorio o están ignoradas por git. Texto propuesto, a continuación de lo propuesto en las entregas 1 y 2. Cada regla distingue la **regla esperada del negocio** de su **nivel de implementación y verificación**, para no prometer garantías que el sistema no ha demostrado.

### B.1 Caso de uso (CU004)

- *Flujo Alternativo* «Continuar conversación», reemplazar la última frase por: «El asistente intenta interpretar las preguntas de seguimiento («¿y cuál fue la de mayor importe?») con los últimos intercambios de la conversación. Si no puede determinar a qué se refiere la pregunta, pide que se reformule; si la pregunta parece requerir otra empresa, sector o año, indica que debe iniciarse una conversación nueva. La respuesta se vuelve a recuperar de los documentos de la empresa y el año de la conversación; sus citas se verifican de forma textual (aparecen literalmente en el fragmento), lo que no equivale a una validación del significado ni a una búsqueda exhaustiva.»
- *Post Condiciones*, añadir: «Aclaración o cambio de contexto: no se guarda ningún mensaje; el usuario reformula y envía de nuevo.»
- *Restricciones*: sustituir «Esta versión no usa el historial como memoria del asistente» por «El historial se usa solo para interpretar la pregunta (hasta los cuatro últimos intercambios y 12 000 caracteres). No es evidencia documental y no se reutilizan respuestas ni citas anteriores. Las comparaciones y los superlativos se limitan a los fragmentos recuperados.»

### B.2 Requisitos y reglas

| Regla esperada del negocio | Implementación y verificación actuales |
|---|---|
| **RF nuevo.** Interpretar las preguntas de seguimiento de una conversación guardada usando su historial reciente. | Implementada. Verificada con **dobles**; evaluación real: muestra pequeña (primera ejecución: 5 de 6 casos conformes; tras el ajuste, 1 repetición del caso afectado conforme; no se midieron los seis casos con el prompt final), insuficiente para afirmar calidad general |
| **RN «El historial no es evidencia».** Si se interpreta un seguimiento, el historial solo se usa para resolver referencias; la respuesta se recupera y verifica de nuevo con los documentos elegibles vigentes. Los mensajes importados o generados antes no se tratan como evidencia ni como instrucciones. | Implementada y probada: el historial no llega al RAG, no se reutilizan citas, y un antecedente repetido por el modelo sin respaldo actual se descarta. Frente a instrucciones incrustadas el modelo solo reduce el riesgo; **no es una garantía absoluta** |
| **RN «Alcance empresarial fijo».** Si la pregunta requiere otra empresa, sector o año, el sistema debe informarlo y exigir una conversación nueva, sin cambiar el alcance. Complementa la RN-037. | **Mitad verificada:** la búsqueda **siempre** usa el contexto de la conversación (probado: ni el modelo ni la pregunta cambian los filtros). **Detección:** en una muestra real de un caso («¿y el año anterior?») el modelo pidió cambiar de contexto; no está verificada en general ni para otra empresa o sector, y depende del modelo. Si no se detecta, la respuesta se busca en el contexto de la conversación |
| **RN «Aclaración».** Si la referencia no se puede determinar con seguridad, el sistema pide aclaración y no consulta ni guarda mensajes. | Implementada (el flujo y la liberación de la reserva están probados). Con el modelo real, en una muestra mínima: aclaró en la ambigüedad entre dos temas; tras una respuesta sin evidencia con tema identificable, la primera pasada presupuso los hechos y, con la regla 9 añadida, la repetición de ese caso (1 solicitud) formuló una consulta condicional. La regla esperada es: si el tema es identificable, consultar sin presuponer lo no confirmado; si no, aclarar. Verificación con el modelo real: insuficiente para generalizar |
| **RN «Alcance de las comparaciones».** Una comparación o un superlativo se limita a lo recuperado; no equivale a una búsqueda exhaustiva sobre todos los documentos de la empresa. | Es una **limitación** del RAG actual (k = 4), documentada; no hay verificación de exhaustividad |
| **RN «Verificación de citas».** Toda afirmación lleva una cita que aparece literalmente en un fragmento recuperado. | Implementada (verificación léxica). **No** garantiza que el pasaje implique la afirmación |
| Ampliar «Procedencia del historial»: la procedencia de un mensaje no cambia por usarse como contexto de interpretación. | Implementada y probada |

RNF a documentar: la interpretación y la generación comparten un plazo global de 240 s; se añade una llamada al modelo por consulta nueva; la evaluación de la interpretación con el modelo real es un criterio de aceptación: una primera muestra de 6 casos dio 5 conformes y 1 discutible; tras el ajuste de la regla de respuestas sin evidencia, 1 repetición conforme; una evaluación más amplia sigue pendiente. Las medidas contra instrucciones incrustadas en el historial reducen el riesgo, no lo eliminan. No afirmar que el sistema «siempre» detecta cambios de año, «nunca» interpreta mal una referencia ni que una cita literal demuestra la afirmación.

### B.3 Modelo de datos

Sin cambios: ni tablas, ni columnas, ni migraciones nuevas en esta entrega (siguen 19 tablas físicas, 16 transaccionales y 3 vectoriales, con el registro de migraciones de cada base). No se persisten resúmenes, preguntas reformuladas ni embeddings del historial.
