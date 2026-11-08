# Historial del Asistente IA — Entrega 1: guardar y recuperar conversaciones

Fecha: 2026-11-07. Estado: implementado y verificado en local; sin staging, commit, push ni despliegue. La migración 0009 **no** se aplicó a QA, UAT ni producción.

## A. Solución aplicada

### Alcance

Solo persistencia y recuperación: guardar una conversación nueva, listar las del usuario autenticado y recuperar una propia. **No** hay continuación mediante IA, memoria conversacional, guardado automático ni edición. La **eliminación de conversaciones queda fuera de alcance**: no pertenece a esta entrega ni a las entregas 2 y 3. Ninguna de estas rutas invoca al LLM, genera embeddings ni consulta la base vectorial, ni depende de su esquema. `POST /asistente/consultas` no cambió y no guarda nada.

### Modelo de datos (migración transaccional 0009)

Los nombres `conversaciones_asistente` y `mensajes_asistente` siguen la convención del proyecto (`analisis_gri_*`, `operaciones_ingesta`). No existía persistencia aprovechable: el servicio actual no conserva lo que responde.

| Tabla | Campos | Garantías de la base |
|---|---|---|
| `conversaciones_asistente` | `id`, `ambiente`, `usuario_id` (FK), `empresa_id` (FK), `sector` (`sector_empresa`), `anio`, `titulo` (≤ 80), `clave_idempotencia`, `huella_contenido` (SHA-256), `creada_en`, `actualizada_en` | `UNIQUE (ambiente, usuario_id, clave_idempotencia)`; CHECK de año (2000–9999), título no vacío, huella hexadecimal de 64 y fechas coherentes; índice de listado `(ambiente, usuario_id, actualizada_en DESC, id DESC)` |
| `mensajes_asistente` | `id`, `conversacion_id` (FK), `orden`, `rol`, `contenido`, `datos_respuesta` (JSONB), `procedencia`, `creado_en` | `UNIQUE (conversacion_id, orden)`; CHECK `orden >= 1`, rol en (`usuario`, `asistente`), contenido no vacío, `datos_respuesta` presente **solo** en mensajes del asistente y de tipo objeto, procedencia en (`APORTADO_POR_CLIENTE`) |

- El orden lo asigna el servidor desde la posición en la lista; el cliente no envía `orden`.
- El título es determinista: la primera pregunta con los espacios colapsados, recortada a 79 caracteres más «…» si pasa de 80.
- Rol y procedencia son `VARCHAR` con CHECK (sin tipos enumerados nuevos). Las claves foráneas no declaran `ON DELETE` porque no hay eliminación.
- Mecanismo existente, como el Módulo 5: modelo fuera de `app.models.__init__`, tablas en `TABLAS_SIN_DDL_AUTOMATICO` (el arranque nunca las crea con `create_all`), migración declarada en `catalogo.py` con sus `objetos` y firmas regeneradas en `firmas.json` (solo se añadieron las dos nuevas). La aplica el arranque (`asegurar_esquema_transaccional`), independiente de la ingesta. Las migraciones 0001–0008 y sus checksums no cambiaron.
- **Compatibilidad central:** no se añadió comprobación a `exigir_esquema_central`, a propósito. Esa comprobación detiene el arranque y se reserva a lo que el inicio de sesión necesita. Si el esquema no pudo prepararse, las tres rutas responden **503** `ASSISTANT_HISTORY_SCHEMA_NOT_READY` (con los detalles ya saneados por el arranque) y el resto de la API sigue disponible.
- La base real detectó un defecto durante las pruebas: SQLAlchemy guardaba `None` como el JSON `null` y no como `NULL` de SQL, lo que violaba el CHECK. Se corrigió con `JSONB(none_as_null=True)`.

#### Respuesta sin evidencia y contenido no vacío

`ConsultaResponse` sin evidencia trae `respuesta: ""` (así la construye `service._sin_evidencia`) y una aclaración en `mensaje`. El CHECK `ck_mensajes_asistente_contenido_no_vacio` exige texto, así que el servidor deriva el contenido legible así: con evidencia, `respuesta`; sin evidencia, `mensaje` (la validación ya exige que la aclaración no esté vacía). No hay defecto: no se relajó ningún CHECK.

| Dato persistido | Valor |
|---|---|
| `mensajes_asistente.contenido` | la aclaración (`mensaje`), p. ej. «No hay documentos publicados de esa empresa, sector y año que permitan responder.» |
| `mensajes_asistente.datos_respuesta` | la respuesta estructurada **íntegra**: `respuesta: ""`, `sin_evidencia: true`, `motivo`, `mensaje`, `afirmaciones`, `citas` y `afirmaciones_descartadas` |

Prueba exacta: `tests/services/test_historial_asistente_postgres.py::test_respuesta_sin_evidencia_persiste_la_aclaracion_como_contenido_y_la_estructura_integra`, parametrizada con los cinco motivos reales de `service._MENSAJES` (construidos con `service._sin_evidencia`). Lee las columnas en PostgreSQL y comprueba `contenido == mensaje`, `datos_respuesta == model_dump` con `respuesta == ""`, y la misma igualdad al recuperar por la ruta. Las pruebas `test_una_respuesta_sin_evidencia_no_trae_texto_afirmaciones_ni_citas_y_si_un_motivo_reconocido` (sin base) y `test_guardar_y_recuperar_conserva_orden_citas_afirmaciones_y_contexto` cubren la validación y el caso mixto.

### Rutas (mismos permisos que el asistente: solo Administrador; sin sesión 401, SuperAdmin 403)

| Ruta | Respuesta |
|---|---|
| `POST /asistente/conversaciones` | **201** conversación creada; **200** reintento idempotente (`reutilizada: true`); 400 empresa inactiva (`COMPANY_INACTIVE`) o de otro sector (`COMPANY_SECTOR_MISMATCH`); 404 empresa inexistente; **409** `CONVERSATION_IDEMPOTENCY_CONFLICT`; **413** `CONVERSATION_TOO_LARGE`; 422 estructura inválida |
| `GET /asistente/conversaciones?pagina=1&tamano=20` | Metadatos paginados (`pagina` base 1, `tamano` 1–100, `total`), de la más reciente a la más antigua; **sin mensajes** |
| `GET /asistente/conversaciones/{conversacion_id}` | Conversación con mensajes por `orden`; **404** uniforme `CONVERSATION_NOT_FOUND` |

El propietario sale de la sesión autenticada y el ambiente de `APP_ENV`; el cliente no envía `usuario_id` ni `ambiente` (cualquier campo extra se rechaza con 422).

**Petición** (`POST /asistente/conversaciones`). El mensaje del asistente es el cuerpo **exacto** que devolvió `POST /asistente/consultas` (esquema `ConsultaResponse` reutilizado); el servidor deriva el `contenido` de ese cuerpo.

```json
{
  "clave_idempotencia": "5b1c6f0e-3f0a-4a53-9d0e-0a8f4a9a7c11",
  "empresa_id": "0d3f0f2e-8d44-4d8e-9a5b-6a1b3f0f9c10",
  "sector": "MINERIA",
  "anio": 2025,
  "mensajes": [
    {
      "rol": "usuario",
      "contenido": "¿Cómo evolucionó el consumo de agua?"
    },
    {
      "rol": "asistente",
      "respuesta": {
        "respuesta": "La empresa redujo su consumo de agua. [1]",
        "afirmaciones": [
          {
            "texto": "La empresa redujo su consumo de agua.",
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
      }
    }
  ]
}
```

**Respuesta** (201). `respuesta` repite la estructura enviada, con sus citas; las marcas de tiempo son de ejemplo.

```json
{
  "id": "9c5a2b7e-1d4f-4a86-b3c0-5e7f8a9b0c12",
  "titulo": "¿Cómo evolucionó el consumo de agua?",
  "contexto": {
    "empresa_id": "0d3f0f2e-8d44-4d8e-9a5b-6a1b3f0f9c10",
    "empresa_nombre": "Minera Andina",
    "empresa_activa": true,
    "sector": "MINERIA",
    "anio": 2025
  },
  "total_mensajes": 2,
  "creada_en": "2026-11-07T15:04:05Z",
  "actualizada_en": "2026-11-07T15:04:05Z",
  "mensajes": [
    {
      "id": "a1b2c3d4-0001-4000-8000-000000000001",
      "orden": 1,
      "rol": "usuario",
      "contenido": "¿Cómo evolucionó el consumo de agua?",
      "respuesta": null,
      "procedencia": "APORTADO_POR_CLIENTE",
      "creado_en": "2026-11-07T15:04:05Z"
    },
    {
      "id": "a1b2c3d4-0002-4000-8000-000000000002",
      "orden": 2,
      "rol": "asistente",
      "contenido": "La empresa redujo su consumo de agua. [1]",
      "respuesta": {
        "respuesta": "La empresa redujo su consumo de agua. [1]",
        "afirmaciones": [
          {
            "texto": "La empresa redujo su consumo de agua.",
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
      "procedencia": "APORTADO_POR_CLIENTE",
      "creado_en": "2026-11-07T15:04:05Z"
    }
  ],
  "reutilizada": false
}
```

Un elemento del listado tiene `id`, `titulo`, `contexto`, `total_mensajes`, `creada_en` y `actualizada_en`.

### Validación y límites

| Regla | Valor |
|---|---|
| Mensajes | 2 a 40, en pares pregunta/respuesta que empiezan por `usuario`; el orden de la lista es el de la conversación |
| Roles | solo `usuario` y `asistente`; `system`, `developer`, `tool` u otro → 422 |
| Pregunta | no vacía, ≤ 2 000 caracteres; sin caracteres NUL |
| Respuesta | texto ≤ 20 000; ≤ 50 afirmaciones; ≤ 20 citas; extracto ≤ 300; textos y nombres acotados |
| Consistencia | con evidencia: sin `motivo`, con afirmaciones y citas, referencias de las afirmaciones existentes y toda cita usada; sin evidencia: sin texto, afirmaciones ni citas, con motivo reconocido y aclaración |
| Contexto | una sola empresa, sector y año; toda cita debe ser de esa empresa y ese año; la empresa debe existir, estar activa y ser del sector (mismas reglas que las consultas) |
| Tamaño total | contenido canónico ≤ 1 000 000 bytes → 413. Con estos límites una conversación admitida se recupera completa en una respuesta acotada |

Los errores 422 describen la regla incumplida y **no repiten el contenido recibido**; el manejador global no devuelve `input`. Los registros del servidor solo llevan identificadores y conteos.

### Procedencia del contenido

El asistente no conserva lo que responde, así que el historial lo aporta el frontend. Validar la estructura **no demuestra** que el backend lo generó ni que sus citas se verificaron originalmente (tampoco que los documentos citados existan). Por eso cada mensaje lleva `procedencia = APORTADO_POR_CLIENTE`, fijada por el servidor, y no debe presentarse como respuesta autenticada, validación oficial ni registro de auditoría. No se añadió firma, caché ni almacenamiento temporal.

### Idempotencia, atomicidad y concurrencia

- Conversación y mensajes se guardan en **una transacción**; un fallo intermedio no deja nada.
- La clave (`clave_idempotencia`, UUID que genera el frontend) está delimitada por usuario y ambiente. Misma clave y mismo contenido → se devuelve lo guardado (200, sin revalidar la empresa: es una repetición); contenido distinto (mensajes, empresa, sector o año) → 409.
- Solicitudes simultáneas: decide la restricción única; la perdedora captura la violación, deshace y resuelve como una repetición o un conflicto.

### Contrato para el frontend

- `clave_idempotencia` se genera **una sola vez por intento de guardado del mismo contenido** y se conserva en todos sus reintentos (red caída, timeout, 5xx) y en los dobles clics. Solo se genera una clave nueva cuando el contenido a guardar es otro (otros mensajes, empresa, sector o año). Reutilizarla con otro contenido produce 409.
- Enviar `empresa_id`, `sector`, `anio` y `mensajes` en pares `usuario`/`asistente` (máximo 40), con el cuerpo exacto de `/asistente/consultas` como `respuesta` de cada mensaje del asistente. No enviar `usuario_id`, `ambiente`, `orden` ni `procedencia`: se rechazan.
- Tratar 200 y 201 como éxito (200 = repetición ya guardada); manejar 409, 413, 422, 400/404 (empresa) y 503.
- Mostrar `procedencia` y `contexto.empresa_activa`; no ofrecer nuevas consultas en conversaciones de empresas inactivas.

### Empresa desactivada después del guardado

Recuperar es historial y sigue permitido. `contexto.empresa_activa` informa el estado **actual** (y `empresa_nombre` el nombre actual). Eso **no** autoriza nuevas consultas: `POST /asistente/consultas` sigue respondiendo `COMPANY_INACTIVE`. Guardar una conversación **nueva** de una empresa inactiva se rechaza (400); reintentar una ya guardada devuelve la guardada.

### Notas para la entrega 2 (documentadas, no implementadas)

1. **Conservar la huella inicial al añadir mensajes.** `huella_contenido` es el SHA-256 del contenido con el que se creó la conversación y es la que resuelve la idempotencia de `POST /asistente/conversaciones`. Al añadir mensajes no debe recalcularse ni sobrescribirse: si cambiara, reintentar el guardado original con su misma clave devolvería un falso 409. La entrega 2 debe definir qué devuelve ese reintento (lo guardado ahora o el estado inicial) y, si necesita detectar cambios posteriores, usar otra columna.
2. **Definir el límite de mensajes de las conversaciones continuadas.** Hoy el tope de 40 mensajes y 1 000 000 bytes es **por petición de guardado**, y de él depende que `GET /asistente/conversaciones/{id}` devuelva una respuesta acotada. La base no limita el total de mensajes de una conversación. Con continuación, la entrega 2 debe fijar un máximo total (mensajes y bytes) y, si lo supera, rechazar con un error explícito o paginar los mensajes de la lectura.
3. **Distinguir mensajes importados y generados por el servidor.** Los mensajes de la entrega 1 son `APORTADO_POR_CLIENTE` y así deben permanecer (no reclasificarlos). Los que genere y persista el servidor necesitan otro valor de `procedencia`, lo que exige una migración nueva que amplíe el CHECK `ck_mensajes_asistente_procedencia`, sin editar la 0009. El historial importado nunca debe usarse como contexto verificado para el modelo.

### Pruebas (locales, PostgreSQL 17.4 aislado; sin proveedores reales)

- `test_historial_asistente.py` (sin base de datos): **37 pasaron** — título, estructura, roles, consistencia de citas, huella, 503 con el esquema sin preparar y ausencia de dependencias de IA en las rutas.
- `test_historial_asistente_postgres.py`: **35 pasaron** (30 de la verificación anterior más las 5 de la respuesta sin evidencia por motivo; ver arriba). Cubren guardado y recuperación con orden y citas, propietario y ambiente fijados por el servidor, tamaño máximo (40 mensajes), listado paginado y aislado por propietario y ambiente, 404 uniforme, 401/403, rechazos sin dejar filas, 413, contexto, empresa desactivada, atomicidad con un fallo inyectado tras insertar la conversación, idempotencia repetida, conflicto, alcance por propietario y ambiente, y dos pruebas con barrera que **fuerzan** la carrera para que decida la restricción única (se midió que pasan por esa rama en 3 de 3 ejecuciones; las pruebas «simultáneas» por HTTP por sí solas no lo garantizan y así se indica en el archivo), ausencia de llamadas a IA, `POST /asistente/consultas` intacto sin guardar nada, migración sobre el esquema anterior con datos (se conservan; checksums 0001–0008 sin cambio; el arranque siguiente no ejecuta nada), instalación nueva y las restricciones de la base probadas con SQL directo.
- Regresión relacionada (ejecutada antes de este ajuste, que solo añade una prueba): asistente (router, servicio, PostgreSQL), alcance ZAP, motor de migraciones simulado y real, esquema central, auditoría, análisis GRI, reportes, `crear_tablas` y contrato: pasaron (sin PostgreSQL 266 pasaron y 1 omitida; con PostgreSQL 101 + 111 + 35 pasaron). Se actualizaron las aserciones que fijaban la lista de migraciones (1–8 → 1–9), el conjunto de tablas sin DDL automático, los recuentos del alcance de ZAP (25 incluidas, 15 excluidas) y la versión «desconocida» de una prueba del motor (9 → 10), sin relajar ninguna condición.
- ZAP: `operaciones.json` clasifica las tres operaciones (POST excluida por crear datos persistentes que no se pueden retirar; los dos GET incluidos) y el fixture `openapi-real.json` se regeneró **solo con adiciones** (2 rutas, 10 esquemas; se comprobó que nada preexistente cambió). 63 pruebas Node de alcance y contrato pasaron. No son un escaneo ZAP real.

### Pendientes reales

- **No ejecutado:** pruebas con pgvector/PostgreSQL en contenedor (Docker detenido): 9 vectoriales de `test_migraciones.py`, 7 variantes `[contenedor]` de `test_migraciones_not_null.py`, `test_fragmentos_vectoriales.py` y las firmas vectoriales; la regresión completa; Jenkins, Sonar, un escaneo ZAP real y QA/UAT.
- **Dependencia con el sector «Otros»:** la migración 0009 va numerada después de la 0008 (sector `OTROS`), que sigue sin commitear en el árbol. Deben integrarse en ese orden.
- **Frontend:** no se tocó (contrato arriba).
- **Entregas 2 y 3:** continuar con IA, memoria conversacional y mensajes generados por el servidor con otra procedencia siguen sin implementar (notas arriba). La eliminación de conversaciones no se planifica en ninguna de ellas.

## B. Cambios necesarios en Análisis y Diseño

No se actualizó ningún documento: las fuentes disponibles (`igualab_md/01-Mockups-y-Propuestas`, en LaTeX y Markdown, con numeraciones distintas) son externas al repositorio o están ignoradas por git. Texto propuesto, con las ubicaciones del LaTeX (los números de RF y RN nuevos los asigna el equipo):

### B.1 Caso de uso del asistente

`s07.tex`, CU004 (líneas 107–120): ampliar el título a «Consulta al Asistente de IA (RAG) y su historial».

- *Flujo Alternativo*, añadir: «**Guardar conversación.** Tras recibir una o más respuestas, el Administrador pulsa “Guardar conversación”. El sistema valida el contexto (empresa, sector y año), guarda la conversación completa en una sola operación y la incorpora al historial. Repetir la acción no crea duplicados. **Recuperar conversación.** El Administrador abre su historial, ve sus conversaciones guardadas (título, empresa, sector, año, fecha) y selecciona una para ver sus preguntas, respuestas y citas.»
- *Post Condiciones*, añadir: «Éxito (guardar): la conversación queda guardada y es visible solo para su propietario. Éxito (recuperar): se muestran los mensajes en su orden con sus citas. Fallo: no se guarda nada parcial.»
- *Restricciones*, añadir: «El historial guardado proviene de lo que el cliente envía: el sistema valida su estructura, pero no lo presenta como respuesta autenticada ni como registro de auditoría. Recuperar una conversación de una empresa desactivada es posible, pero no permite nuevas consultas sobre ella. Esta versión no continúa conversaciones con el asistente ni elimina conversaciones; la eliminación queda fuera de alcance.»

### B.2 Requisitos y reglas

`rf.tex` (tras RF-023/RF-024) y `rn.tex` (tras RN-037):

- RF nuevo: «Guardar una conversación con el asistente de IA en el historial del usuario.»
- RF nuevo: «Listar y recuperar las conversaciones guardadas, con sus mensajes, citas y contexto de empresa, sector y año.» (trazar a RN-037 y a las RN nuevas).
- RN nueva «Propiedad y alcance del historial»: «SI un Administrador guarda una conversación, ENTONCES el historial es de su propiedad, se limita a un ambiente, una empresa, un sector y un año, y ningún otro usuario puede listarla ni recuperarla. SE APLICA A el historial del asistente. SE BASA EN la confidencialidad de las consultas. Y TIENE LA SIGUIENTE EXCEPCIÓN la empresa desactivada conserva sus conversaciones consultables, sin habilitar nuevas consultas.»
- RN nueva «Procedencia del historial»: «SI el historial proviene del cliente, ENTONCES el sistema lo marca como aportado por el cliente y no lo presenta como respuesta autenticada ni validación oficial. EXCEPCIÓN los mensajes que el propio servidor genere y persista tendrán una procedencia distinguible (entregas siguientes).»
- RN-037 no cambia; añadir en su aplicación que una conversación conserva un único contexto.

### B.3 Permisos

`s07.tex:116` (Restricciones de CU004): sustituir «Solo el rol Administrador tiene la capacidad de interactuar con el agente de IA.» por «Solo el rol Administrador puede interactuar con el agente de IA y con su historial; cada Administrador ve únicamente sus propias conversaciones. El SuperAdmin no tiene acceso.»

### B.4 Modelo de datos y recuento de tablas

`s08-13.tex`:

- Dominio «ASISTENTE RAG» (línea ~41): **sustituir** `consultas_asistente` y `consulta_citas` (y su propósito) por `conversaciones_asistente` y `mensajes_asistente` — «Historial guardado de conversaciones y mensajes con sus citas y su procedencia». Las dos tablas antiguas **no existen en el código** y no se crean por esa descripción: no se añade ninguna tabla adicional. Retirar también sus entradas del diccionario (líneas ~270–293).
- **Recuento verificado contra las migraciones actuales: 18 tablas físicas, 15 transaccionales y 3 vectoriales, incluido el registro de migraciones de cada base.**

| Base | Tablas | Total |
|---|---|---|
| Transaccional | `usuarios`, `sesiones`, `tokens_recuperacion`, `empresas`, `auditoria`, `reportes_prospeccion`, `documentos`, `operaciones_ingesta`, `analisis_gri_control`, `analisis_gri_estados`, `analisis_gri_historial`, `analisis_gri_validaciones`, `conversaciones_asistente`, `mensajes_asistente`, `igualab_migraciones` | 15 |
| Vectorial | `fragmentos_documento`, `cierres_documento`, `igualab_migraciones` | 3 |

  Cómo se verificó: en un PostgreSQL aislado se aplicaron las migraciones 0001–0009 sobre la base de prerrequisitos y quedaron 13 tablas; faltan `sesiones` y `tokens_recuperacion`, que crea el arranque con `create_all` (no están en `TABLAS_SIN_DDL_AUTOMATICO`) y el helper de pruebas no crea. Las tablas vectoriales y su registro se cuentan por las migraciones vectoriales 0001 y 0002 y el motor (`igualab_migraciones`); **no se ejecutaron** porque Docker estaba detenido. El total actual del documento («13; 12 en la transaccional y 1 en la vectorial») debe reconciliarse con esta lista.
- Añadir los diccionarios `\tabname{conversaciones\_asistente}` y `\tabname{mensajes\_asistente}` con los campos, tipos y restricciones de la tabla de la parte A.
- Línea 99 («CHECK estados ENUM»): añadir `rol` y `procedencia` de `mensajes_asistente` como VARCHAR con CHECK, y las restricciones únicas `(ambiente, usuario_id, clave_idempotencia)` y `(conversacion_id, orden)` en el listado de restricciones.

### B.5 Trazabilidad

CU004 (`s01-06.tex:65` y `s08-13.tex:411`): añadir los RF y RN nuevos a la fila del Asistente de IA.
