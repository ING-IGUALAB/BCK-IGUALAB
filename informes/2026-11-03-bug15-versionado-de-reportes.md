# BUG-15 · Versionado de reportes de prospección tras una nueva validación GRI

Rama `development`. Alcance: backend y su documentación. Este informe vive en `informes/` (versionable). `docs/` está ignorado por git y el documento oficial de Análisis y Diseño no está disponible para editarlo: la parte B entrega las sustituciones listas para incorporar, sin afirmar que se hayan aplicado.

## A. Solución aplicada

### A.1 Versionado por empresa y año

* Una empresa y un año admiten varias **versiones** del reporte (`reportes_prospeccion.version`, entero ≥ 1). Cada versión emitida es una fila nueva e **inmutable** con su PDF (`pdf`, bytes y `sha256`), su snapshot (`snapshot`, JSONB del análisis validado que la originó), su fecha de emisión (`creado_en`, la misma que figura en el PDF), su autor y la validación que la respalda (`validacion_id`; es nulo solo en los reportes históricos anteriores al versionado). La **última versión emitida** es la de mayor versión; eso no implica que esté actualizada (ver A.6).
* Tres números que no son lo mismo: la **versión del reporte** (ordinal de la emisión), la **validación** (`analisis_gri_validaciones`, número e identificador de la confirmación humana) y la **versión de estados** del análisis (`analisis_gri_control.version`, cuenta los cambios de estado y define si una validación sigue vigente).
* **Reutilización por validación.** Una validación respalda como mucho un reporte. Una solicitud repetida para la misma validación no crea otra versión: devuelve la ya emitida (`POST /reportes` responde 200 con `reutilizado = true`; 201 al crear). Una validación nueva y vigente habilita la versión siguiente sin alterar las anteriores. Cambiar estados GRI sin volver a validar deja el análisis sin validación vigente y la emisión se rechaza (`ANALYSIS_NOT_VALIDATED`).
* **Numeración.** La versión se asigna como `max + 1` bajo un candado asesor transaccional por empresa/año, con `UNIQUE(empresa_id, anio, version)` como última defensa; si el número candidato dejó de ser el siguiente se vuelve a renderizar (hasta 3 intentos) y, si no se estabiliza, `REPORT_VERSION_CONFLICT`. Solo se traducen a conflicto las violaciones de `uq_reporte_validacion` (se reutiliza) y de `uq_reporte_empresa_anio_version`; cualquier otra `IntegrityError` se propaga.
* **Conservación.** La descarga por identificador (`GET /reportes/{id}/archivo`) entrega siempre los bytes almacenados de esa versión; nunca se recompila ni se reconstruye con datos actuales. Un cambio posterior (nombre de empresa, nombre de usuario, nuevos documentos) no modifica una emisión ya confirmada.

### A.2 Flujo de emisión (`generar_reporte`)

1. **Preparación sin bloqueos** (`_preparar`): `obtener_analisis_validado` exige validación vigente y captura el snapshot; si ya existe la emisión de esa validación se devuelve (200) sin renderizar; si no, versión candidata = `max + 1`.
2. **Renderizado** del PDF sin transacción ni bloqueo abiertos.
3. **Transacción final corta** (`_publicar`): toma la protección común del origen (A.3), recomprueba que la validación sigue siendo la última y vigente (versión de estados y huella de documentos), reutiliza si otra solicitud ya emitió, toma el candado de numeración, asigna la versión e inserta reporte y evento de auditoría en la misma transacción. Si el origen dejó de ser válido: `REPORT_SOURCE_CHANGED` y no se confirma el PDF candidato.
4. Un fallo del PDF o de la persistencia no escribe nada: las versiones anteriores quedan intactas.

La vigencia se exige **al confirmar** una emisión nueva. Una incorporación de documentos posterior a su confirmación no modifica la emisión histórica; solo deja la validación no vigente para lo que se haga después.

### A.3 Coordinación con ingesta, validación y desactivación de empresas

Una sola función, `proteger_origen_de_analisis` (`app/modules/analisis_gri/application/coordinacion.py`), recibe la sesión del llamador y no hace commit ni rollback. Adquiere, dentro de su transacción: (1) la fila de la **empresa en modo compartido** (`FOR SHARE`) y (2) la fila de **`analisis_gri_control`** de (ambiente, empresa, año) con `FOR UPDATE` (creándola si falta con `INSERT … ON CONFLICT DO NOTHING`). Es un candado de PostgreSQL: no hay candados en memoria, Redis ni infraestructura adicional.

| Camino | Uso |
|---|---|
| Ingesta (`publicacion._confirmar_publicacion`, también en reintentos y recuperación) | Tras las llamadas externas, en una transacción nueva toma la protección, hace el `UPDATE` condicional de `vector_publicado_en` (el instante en que un documento pasa a ser elegible) y confirma. Idempotente. |
| Asignación de estado y validación GRI | Toman la protección antes de la lectura definitiva de documentos y estados. |
| Emisión | Solo en la transacción final; el renderizado no retiene bloqueos. |
| Activar o desactivar una empresa (`cambiar_estado_empresa`) | Bloqueo exclusivo de la fila de la empresa (`FOR NO KEY UPDATE`), incompatible con el compartido: espera a las operaciones en curso y las posteriores ven el cambio. Afecta a todos los años sin enumerarlos. |

* **Orden de adquisición** (único en todos los caminos): empresa (compartido) → control (exclusivo) → candado asesor de numeración (solo la emisión) → filas de `documentos` / `reportes_prospeccion` que se escriben. Ningún camino toma una fila de documento y luego la empresa o el control. Empresas y años distintos no se bloquean entre sí.
* El candado asesor de numeración se conserva porque la protección común es por ambiente y la numeración por empresa/año.
* **El bloqueo no sustituye la detección de cambios del origen**: serializa; quien detecta el cambio es la recomprobación de la validación (versión de estados y huella de documentos) con el bloqueo ya tomado. El contador de cambios de estados no se incrementa al incorporar documentos.
* **Bloqueos mutuos.** PostgreSQL detecta y aborta los ciclos de espera en tiempo de ejecución (error 40P01). La **prevención** es del proyecto: el orden único anterior y las pruebas de coordinación de los caminos actuales. Un camino futuro que no respete el orden quedaría protegido solo por la detección del motor.
* Las lecturas (A.7) no toman estos bloqueos.

### A.4 Integridad y migraciones

* **0006 `reportes_versionados`**: columnas `version` (los reportes existentes pasan a 1 sin ejecutar `UPDATE`) y `validacion_id`; `UNIQUE(empresa_id, anio, version)` sustituye a `UNIQUE(empresa_id, anio)`; `UNIQUE(validacion_id)`; `CHECK(version >= 1)`; FK `fk_reporte_validacion`.
* **0007 `integridad_de_reportes`**: `UNIQUE(id, empresa_id, anio)` en `analisis_gri_validaciones` (destino de la FK compuesta), FK compuesta `fk_reporte_validacion_empresa_anio` (la validación debe ser de la misma empresa y año) y el disparador `tr_reportes_exigir_validacion` (BEFORE INSERT: un reporte nuevo no puede insertarse sin validación; los históricos conservan `NULL`).
* Ambas son idempotentes, sin objetos declarados, se registran con el mecanismo existente (checksums) y no desactivan disparadores ni usan `TRUNCATE`/`CASCADE`. Los disparadores de solo inserción de reportes y validaciones permanecen. Si existe un reporte con la validación de otra empresa o año, la 0007 se detiene sin modificar nada (`MIGRATION_DATA_INCOMPATIBLE`, SQLSTATE `IG001`; la consulta para listarlos está en la cabecera de la migración).
* **Control de arranque** (`app/migraciones/compatibilidad.py`): consulta el catálogo de PostgreSQL y, además de la existencia por nombre de los elementos que ya exigía (columna de resultado, valores del tipo de evento y restricción de la auditoría), comprueba para los reportes:
  * **Columnas** `version` (`integer`, no nula) y `validacion_id` (`uuid`, nula): tipo y nulabilidad.
  * **Restricciones revisadas**: tipo, columnas en orden, que estén validadas y, en las claves foráneas, tabla y columnas de destino. Cuando la restricción define una condición (CHECK), se compara su texto sin espacios ni paréntesis con el esperado.
  * **Disparadores**: tabla, nombre, características (momento y evento, por fila; `tgtype`), función que ejecutan y habilitación (se rechaza el desactivado).
  * **Comprobación textual** de que el cuerpo de la función de cada disparador contenga `RAISE EXCEPTION`.
  * La **ausencia** de la unicidad antigua `uq_reporte_empresa_anio`.

  Es una comprobación de definición y de texto, no semántica: que aparezca `RAISE EXCEPTION` no demuestra toda la protección ni detecta cualquier alteración posible del cuerpo de la función (el comportamiento lo cubren las pruebas con PostgreSQL). Un objeto ausente, desactivado o con una definición distinta de la esperada impide el arranque (`CENTRAL_SCHEMA_INCOMPATIBLE`).
* **Históricos**: conservan id, PDF, `sha256`, snapshot, fecha y autor con `version = 1` y `validacion_id = NULL`; nunca se infiere un vínculo.

### A.5 Vista previa

`POST /reportes/vista-previa` exige validación vigente (`ANALYSIS_NOT_VALIDATED`) y:

* **Si esa validación ya está respaldada por una emisión** (por `validacion_id` o, en un histórico, por número y huella guardados en su snapshot): devuelve exactamente el PDF almacenado. No compila, no reconstruye con datos actuales, no crea versión, no modifica el snapshot y no registra evento de generación.
* **Si todavía no tiene emisión**: vista previa **candidata** con el constructor común de la emisión (`_Preparacion.html()`). No persiste ni reserva número; la versión (siguiente número) y la fecha (hoy) que muestra no se prometen a una emisión futura.
* Un histórico de origen no verificable no se presume equivalente a la validación: se trata como «sin emisión».

### A.6 Disponibilidad y significado de los campos

Una sola función pura, `versionado.evaluar`, produce `estado`, `puede_generar`, `motivo_no_generar`, `reporte_actualizado`, `actualizacion_pendiente` y `validacion_anterior_no_vigente`; la disponibilidad, la consulta de la última emisión y la descarga la comparten, de modo que no pueden contradecirse. La presencia de un reporte **no oculta** el estado actual del análisis (su `reporte_id`/`reporte_version` quedan como referencia).

| Campo | Significado |
|---|---|
| `reporte_actualizado` | `true` solo si se **demuestra** que la última versión respalda la última validación **y** esa validación sigue vigente. Con cambios por revisar o validar es `false` aunque corresponda a la última validación registrada. `null` sin reporte. |
| `actualizacion_pendiente` | Existe una validación posterior a la que respalda el reporte y aún no se emitió (también si esa validación ya dejó de ser vigente). No se activa por cualquier cambio ni por un origen no verificable. |
| `validacion_anterior_no_vigente` | Hay una validación registrada y el análisis actual ya no la hace vigente (estados, documentos o elegibilidad cambiaron). |
| `origen_verificable` | Se sabe qué validación respalda el reporte (guarda `validacion_id` o, en un histórico, su snapshot conservó número y huella). `false` en un histórico sin esos datos. |
| `puede_generar` | `true` exactamente en `VALIDACION_VIGENTE` y `ACTUALIZACION_PENDIENTE`. Con `REPORTE_GENERADO` es `false`: no hay nada nuevo que emitir (repetir `POST /reportes` devuelve la existente). |

| Situación | `estado` | `puede_generar` | `motivo_no_generar` | `reporte_actualizado` | `actualizacion_pendiente` |
|---|---|---|---|---|---|
| Sin reporte, con validación vigente | `VALIDACION_VIGENTE` | true | – | null | false |
| Reporte que respalda la última validación vigente | `REPORTE_GENERADO` | false | `REPORTE_ACTUALIZADO` | true | false |
| Filas GRI sin estado | `PENDIENTE_REVISION` | false | `PENDIENTE_REVISION` | false (con reporte) | false |
| Revisado, sin validación vigente | `PENDIENTE_VALIDACION` | false | `PENDIENTE_VALIDACION` | false (con reporte) | false |
| Validación vigente posterior sin emitir | `ACTUALIZACION_PENDIENTE` | true | – | false | true |
| Validación posterior que dejó de ser vigente | `PENDIENTE_REVISION` / `PENDIENTE_VALIDACION` | false | el del flujo | false | true |
| Sin códigos GRI evaluables | `SIN_CODIGOS_GRI` | false | `SIN_CODIGOS_GRI` | false (con reporte) | false |
| Sin documentación elegible (incluye un año con reportes) | `SIN_DOCUMENTACION_ELEGIBLE` | false | `SIN_DOCUMENTACION_ELEGIBLE` | false (con reporte) | false |
| Histórico de origen no verificable con validación vigente | `VALIDACION_VIGENTE` | true | – | false | false |

### A.7 Históricos de origen no verificable y lecturas coherentes

* Se conservan con `validacion_id` nulo y siguen consultables y descargables por identificador (el archivo no cambia). La compatibilidad por número y huella se acepta solo cuando ambos datos están en el snapshot y coinciden; con datos incompletos no se inventa un vínculo ni se declara actualizado. Una validación vigente sin emisión verificablemente asociada permite emitir una versión nueva y el histórico se conserva. Nada se escribe ni se recalcula al consultar.
* **Lectura coherente.** Disponibilidad, consulta de la última emisión y descarga actualizada leen la emisión, la última validación y el análisis en **una sola transacción de instantánea** (`REPEATABLE READ`, `_leer_origen`), fijada antes de la primera consulta de esa transacción y solo para ella: el nivel de aislamiento no se modifica globalmente y se restablece al devolver la conexión. Todo lo evaluado corresponde al instante de su primera consulta; no se promete que siga siendo lo último después de él. Son lecturas sin bloqueos: no toman la protección de A.3 ni serializan consultas independientes, y no retienen nada durante el envío del archivo. La transacción no es de solo lectura: la descarga registra su auditoría en ella.

### A.8 Descarga actualizada e histórica

* **Por identificador** — `GET /reportes/{id}/archivo`: los bytes exactos de esa emisión (registra `DESCARGA`); `REPORT_NOT_FOUND` si no existe.
* **Actualizada** — `GET /reportes/vigente/archivo`: entrega los bytes almacenados de la última versión solo si `reporte_actualizado`. Si no, 409 con un código por causa real y la prioridad siguiente cuando coinciden varias:
  1. `REPORT_UPDATE_PENDING` — existe una validación posterior sin emitir.
  2. `REPORT_ORIGIN_UNVERIFIABLE` — histórico cuyo origen no puede verificarse (nunca se responde `REPORT_UPDATE_PENDING` sin una validación posterior real).
  3. `REPORT_ANALYSIS_NOT_CURRENT` — cambios por revisar o validar, sin códigos GRI o sin documentación elegible.

  Sin reportes: `REPORT_NOT_FOUND`. Los `details` llevan solo identificadores, números y códigos: `reporte_id`, `version`, `ultima_validacion_numero`, `origen_verificable`, `estado`, `puede_generar`, `motivo_no_generar` y `accion_sugerida`: `EMITIR` si `puede_generar`; si no, `REVISAR` (filas sin estado), `VALIDAR` (revisado, falta validar) o `DESCARGAR_VERSION_HISTORICA`. Nunca se sugiere `VALIDAR` si primero faltan estados por revisar ni `EMITIR` sin una validación vigente.
* Una descarga exitosa registra el evento `DESCARGA` en la misma transacción de la lectura; un rechazo no registra ningún evento de éxito; un fallo de auditoría se propaga (nunca responde 200 con el archivo si no pudo auditarse).
* La consulta `GET /reportes/vigente` describe también a un histórico no verificable sin fallar.

### A.9 Determinismo: contenido, presentación y bytes

* **Contenido**: el contenido de una versión se obtiene solo de su snapshot; el mismo snapshot, versión y fecha producen el mismo HTML y el mismo texto.
* **Presentación**: la plantilla vigente al emitir define la presentación; un reporte ya emitido la conserva porque se almacenan sus bytes. No se afirma igualdad visual entre emisiones hechas con plantillas distintas.
* **Bytes**: no se garantizan. Compilar dos veces el mismo HTML con WeasyPrint produce PDFs con bytes (y `sha256`) distintos pero el mismo texto extraído. Por eso cada versión guarda y entrega sus propios bytes y su `sha256`.

### A.10 Contrato HTTP final

Autenticación y permisos: solo Administrador (401 sin sesión, 403 SuperAdmin) en todas las rutas.

| Ruta | Comportamiento |
|---|---|
| `POST /reportes` | 201 versión creada · 200 versión reutilizada. Cuerpo `ReporteEmision` = `ReporteResumen` + `reutilizado`. Errores: 404 `COMPANY_NOT_FOUND`, 409 `ANALYSIS_NOT_VALIDATED` / `REPORT_SOURCE_CHANGED` / `REPORT_VERSION_CONFLICT`, 502 `PDF_GENERATION_ERROR`. |
| `POST /reportes/vista-previa` | PDF almacenado de la emisión existente, o candidata (A.5). |
| `GET /reportes` | Todas las versiones, la más reciente primero, con `version`, `es_ultima_version`, `validacion_id`, `validacion_numero`. Filtros opcionales `empresa_id`, `anio`, `limite` (1–500), `desplazamiento`. |
| `GET /reportes/vigente?empresa_id&anio` (en la ruta, «vigente» significa la última versión emitida, no que esté actualizada) | `ReporteVigente`: `ReporteResumen` + `ultima_validacion_id`, `ultima_validacion_numero`, `origen_verificable`, `validacion_anterior_no_vigente`, `reporte_actualizado`, `actualizacion_pendiente`, `puede_generar`, `motivo_no_generar`. 404 `REPORT_NOT_FOUND`. |
| `GET /reportes/vigente/archivo?empresa_id&anio` | A.8. Cabecera `X-Reporte-Version` (CORS no la expone: la versión está en `/vigente`). |
| `GET /reportes/{reporte_id}/archivo` | Bytes exactos de esa versión. |
| `GET /reportes/disponibilidad?empresa_id[&anio]` | Años con documentos elegibles o reportes: `anio`, `estado`, `puede_generar`, `motivo_no_generar`, `documentos_elegibles`, `filas_gri`, `filas_sin_estado`, `validacion_anterior_no_vigente`, `reporte_id`, `reporte_version`, `reporte_creado_en`, `reporte_validacion_numero`, `versiones_emitidas`, `ultima_validacion_numero`, `origen_verificable`, `reporte_actualizado`, `actualizacion_pendiente`. |
| Análisis GRI (`ValidacionVista`) | Campo aditivo `id` (identificador de la validación). |

Ejemplos (campos relevantes).

Primera emisión — `POST /reportes` → `201`:
```json
{"id": "6f0c1d9e-…", "empresa_id": "b3a9c2d4-…", "empresa_nombre": "Empresa de ejemplo S.A.", "anio": 2022, "version": 1,
 "es_ultima_version": true, "validacion_id": "0a4e7c55-…", "validacion_numero": 1, "generado_por_nombre": "Ana Admin",
 "sha256": "9c1f…", "creado_en": "2026-11-03T15:20:11Z", "reutilizado": false}
```
Repetida → `200` con el mismo `id` y `"reutilizado": true`.

Nueva validación pendiente de emitir — `GET /reportes/vigente`:
```json
{"id": "6f0c1d9e-…", "version": 1, "validacion_numero": 1, "ultima_validacion_numero": 2, "origen_verificable": true,
 "validacion_anterior_no_vigente": false, "reporte_actualizado": false, "actualizacion_pendiente": true,
 "puede_generar": true, "motivo_no_generar": null}
```
`GET /reportes/vigente/archivo` → `409`:
```json
{"error": {"code": "REPORT_UPDATE_PENDING", "message": "Existe una validación posterior a la última versión emitida: …",
 "details": {"reporte_id": "6f0c1d9e-…", "version": 1, "ultima_validacion_numero": 2, "origen_verificable": true,
             "estado": "ACTUALIZACION_PENDIENTE", "puede_generar": true, "motivo_no_generar": null, "accion_sugerida": "EMITIR"},
 "request_id": "…"}}
```
Cambios sin validar — `reporte_actualizado: false`, `actualizacion_pendiente: false`, `validacion_anterior_no_vigente: true`, `motivo_no_generar: "PENDIENTE_VALIDACION"`; la descarga actualizada → `409 REPORT_ANALYSIS_NOT_CURRENT` con `accion_sugerida: "VALIDAR"`.

Histórico de origen no verificable con validación vigente — `GET /reportes/vigente`: `{"validacion_id": null, "origen_verificable": false, "reporte_actualizado": false, "actualizacion_pendiente": false, "puede_generar": true}`; la descarga actualizada → `409 REPORT_ORIGIN_UNVERIFIABLE`; `POST /reportes` → `201` con la versión siguiente y el histórico intacto.

Reporte existente con la empresa renombrada — la vista previa devuelve los mismos bytes que `GET /reportes/{id}/archivo`; `GET /reportes` ya muestra el nombre nuevo.

### A.11 Archivos y funciones principales

* Reportes: `app/modules/reportes/service.py` (`generar_reporte`, `_preparar`, `_publicar`, `vista_previa`, `_leer_origen`, `_evaluacion`, `obtener_reporte_vigente`, `obtener_pdf_vigente`, `disponibilidad_de_reportes`, `obtener_pdf`), `versionado.py` (`evaluar`, `respalda`, `origen_verificable`, `rechazo_de_descarga`, `accion_sugerida`), `schemas.py`, `router.py`, `domain/plantilla.py` y `plantillas/reporte_prospeccion.html` (versión y fecha de emisión en el PDF).
* Coordinación: `app/modules/analisis_gri/application/coordinacion.py` (`proteger_origen_de_analisis`); `…/servicio.py` (`confirmar_validacion_vigente`, `ultimas_validaciones`, `resumen_por_anio` con `liberar`); `app/modules/ingesta/application/documento_service/publicacion.py` (`_confirmar_publicacion`); `app/modules/empresas/service.py` (`cambiar_estado_empresa`).
* Esquema: `app/migraciones/transaccional/0006_reportes_versionados.sql`, `0007_integridad_de_reportes.sql`, `catalogo.py`, `compatibilidad.py`, `motor.py` (`MIGRATION_DATA_INCOMPATIBLE`); modelos `app/models/reportes.py` y `analisis_gri.py`.
* Consumidores de prueba: fixture OpenAPI y alcance de `pruebas-zap`; colección E2E de `pruebas-http` (pasos E3b–E3e).

### A.12 Limitaciones que afectan al comportamiento final

* La vista previa y la emisión determinan la vigencia en un instante y consultan la emisión en otro; el resultado es correcto para esa validación (la emisión es inmutable y específica de ella), pero no es una instantánea única como en la disponibilidad y la descarga.
* El estado de un año con reportes y sin documentación elegible es `SIN_DOCUMENTACION_ELEGIBLE` y `reporte_actualizado = false`; el reporte sigue descargable por identificador.
* El disparador de la 0007 no actúa sobre filas existentes (por diseño) y un propietario de la tabla podría quitarlo; el arranque lo detecta.
* La elegibilidad de documentos cambia solo por la confirmación de `vector_publicado_en` y por la actividad de la empresa; si otra operación futura la cambia, debe llamar a `proteger_origen_de_analisis`.
* El PDF no es reproducible byte a byte (A.9).

## B. Cambios en Análisis y Diseño

Sustituciones para incorporar al documento oficial de Análisis y Diseño, que no está disponible para editarlo. «Texto anterior» es el de las fuentes locales (`docs/fuentes/latex`, históricas y de numeración distinta; la sección «4.2.7» citada para el Módulo 7 no figura con ese número en ellas). No se inventan numeraciones ni se cambian requisitos unilateralmente: son las redacciones que corresponden al comportamiento implementado. Los nombres de columnas son los implementados (`pdf`, `snapshot`); el diccionario anterior usa `pdf_path` y `contenido_snapshot`.

### B.1 RN-040 · Determinismo y generación sin IA

**Texto anterior.** SI se genera un reporte, ENTONCES su contenido se construye exclusivamente con los resultados ya almacenados y validados, sin invocar al servicio de IA. SE APLICA A la generación de reportes. SE BASA EN la reproducibilidad del resultado. Y TIENE LA SIGUIENTE EXCEPCIÓN dos generaciones sobre los mismos datos producen el mismo resultado.

**Texto nuevo.** SI se genera un reporte, ENTONCES su contenido se construye exclusivamente con los resultados ya almacenados y validados, sin invocar al servicio de IA. SE APLICA A la generación de reportes. SE BASA EN la reproducibilidad del resultado. Y TIENE LA SIGUIENTE EXCEPCIÓN dos generaciones sobre el mismo snapshot producen el mismo contenido (los mismos datos y el mismo texto); no se garantiza que los bytes del PDF sean idénticos entre compilaciones ni que la presentación de reportes emitidos con plantillas distintas coincida. El PDF de una versión emitida se conserva tal como se generó y la vista previa de esa versión es ese mismo PDF. Una nueva validación implica nuevos datos de origen y puede originar una nueva versión del reporte.

### B.2 RN-041 · Inmutabilidad y visualización del reporte

**Texto anterior.** SI un reporte es generado, ENTONCES este no puede modificarse ni eliminarse, y su historial es visible y descargable por cualquier Administrador. SE APLICA A los reportes de prospección. SE BASA EN la integridad del entregable. Y TIENE LA SIGUIENTE EXCEPCIÓN no se genera un segundo reporte de la misma empresa y año.

**Texto nuevo.** RN-041 · Inmutabilidad, versionado y visualización del reporte. SI un reporte es generado, ENTONCES esa versión no puede modificarse ni eliminarse, conserva su PDF y su snapshot, y el historial de todas las versiones es visible y descargable por cualquier Administrador. SE APLICA A los reportes de prospección. SE BASA EN la integridad del entregable. Y TIENE LAS SIGUIENTES EXCEPCIONES (1) una empresa y año admiten varias versiones: una validación vigente que aún no tiene reporte habilita emitir la versión siguiente sin alterar las anteriores, y una solicitud repetida para la misma validación no crea otra versión, devuelve la ya emitida; (2) toda versión nueva se respalda en una validación de la misma empresa y año, salvo los reportes anteriores al versionado (históricos), que conservan su identificación sin validación vinculada y no se presentan como actualizados si su origen no puede verificarse; (3) si al realizar la comprobación final la validación de origen ya no es vigente, el sistema no confirma la nueva versión; los cambios posteriores a la confirmación (por ejemplo, la incorporación de documentos) no modifican la emisión histórica.

### B.3 Modelo de datos — relaciones

**Texto anterior.** Un reporte de prospección pertenece a una empresa (UNIQUE empresa+anio — RN-041) y congela su snapshot a la fecha de emisión.

**Texto nuevo.** Un reporte de prospección pertenece a una empresa; una empresa y un año admiten varias versiones (UNIQUE empresa+anio+version — RN-041). Cada versión nueva está respaldada por exactamente una validación GRI de la misma empresa y año (clave foránea compuesta validación+empresa+año y UNIQUE validación) y congela su snapshot a la fecha de emisión. Los reportes anteriores al versionado son la versión 1 y conservan `validacion_id` nulo.

### B.4 Diccionario de datos y restricciones — tabla `reportes_prospeccion`

**Texto anterior** (filas afectadas).
* `empresa_id` · UUID (FK/UQ\*) · «Empresa del reporte. Forma una restricción única compuesta con anio.» · NO
* `anio` · SMALLINT (UQ\*) · «Año del reporte. Forma una restricción única compuesta con empresa\_id.» · NO
* `creado_en` · TIMESTAMPTZ · «Fecha y hora de generación del reporte.» · NO

**Texto nuevo.**
* `empresa_id` · UUID (FK/UQ\*) · «Empresa del reporte. Forma una restricción única compuesta con anio y version.» · NO
* `anio` · INTEGER (UQ\*) · «Año del reporte. Forma una restricción única compuesta con empresa\_id y version.» · NO
* `version` · INTEGER (CHECK ≥ 1; UQ\*) · «Versión del reporte dentro de la empresa y el año (1 = primera emisión). Los reportes anteriores al versionado son la versión 1.» · NO
* `validacion_id` · UUID (FK compuesta con empresa\_id y anio → analisis\_gri\_validaciones; UQ) · «Validación GRI que respalda el reporte; pertenece a la misma empresa y año. Obligatoria en todo reporte nuevo (la base rechaza la inserción sin ella); nula solo en los reportes anteriores al versionado.» · SÍ
* `creado_en` · TIMESTAMPTZ · «Fecha y hora de emisión de la versión (la que figura en el PDF).» · NO

**Restricciones de la tabla (texto nuevo).** UNIQUE(empresa\_id, anio, version); UNIQUE(validacion\_id); CHECK(version ≥ 1); FK validacion\_id → analisis\_gri\_validaciones(id); FK compuesta (validacion\_id, empresa\_id, anio) → analisis\_gri\_validaciones(id, empresa\_id, anio); disparador BEFORE INSERT que rechaza un reporte nuevo sin validación; disparador de solo inserción que rechaza UPDATE y DELETE. En `analisis_gri_validaciones`: UNIQUE(id, empresa\_id, anio) como destino de la clave compuesta.

### B.5 Módulo 7 — Generación de Reportes de Prospección

**Texto anterior.** Exclusivo del Administrador: consolida el análisis validado de una empresa en un documento PDF para el acercamiento comercial. Solo puede generarse para empresas con al menos un documento indexado y se construye exclusivamente a partir de los resultados de análisis ya almacenados y validados manualmente, sin invocar al servicio de IA en este paso, por lo que dos generaciones sobre los mismos datos producen el mismo resultado. El reporte incluye el estado de cada código GRI con su cita de respaldo, las sanciones identificadas (con monto o marcadas como «no cuantificada») y un resumen ejecutivo con el puntaje ESG. Una vez generado, el reporte es inmutable; su historial es visualizable y descargable por cualquier Administrador.

**Texto nuevo.** Exclusivo del Administrador: consolida el análisis validado de una empresa en un documento PDF para el acercamiento comercial. Solo puede generarse para empresas con al menos un documento indexado y se construye exclusivamente a partir de los resultados de análisis ya almacenados y validados manualmente, sin invocar al servicio de IA en este paso, por lo que dos generaciones sobre el mismo snapshot producen el mismo contenido. El reporte incluye el estado de cada código GRI con su cita de respaldo, las referencias a sanciones, los montos detectados y las advertencias correspondientes cuando un monto no se detecta o necesita revisión (el total de montos se presenta solo cuando es completo y fiable; si no, se indica el motivo), un resumen ejecutivo con el puntaje ESG y, de forma visible, su versión y fecha de emisión. Cada validación vigente que aún no tiene reporte da lugar a una versión inmutable del reporte de la empresa y el año, con su PDF, el snapshot del análisis validado y la validación que la respalda (de la misma empresa y año); una solicitud repetida para la misma validación devuelve la versión ya emitida. La última versión emitida es la de mayor número; no implica que esté actualizada. Si al realizar la comprobación final la validación de origen ya no es vigente, el sistema no confirma la nueva versión; los cambios posteriores a la confirmación no modifican la emisión histórica. La vista previa de una versión ya emitida entrega su PDF almacenado; la de una validación sin reporte es una vista previa candidata que no se guarda ni reserva número. El sistema solo presenta un reporte como actualizado cuando puede demostrarse que respalda la última validación y que esta sigue vigente respecto del análisis; en cualquier otro caso informa si falta revisar, validar o emitir, o si el origen del reporte no puede verificarse, y la versión anterior sigue disponible por su identificador. El historial de todas las versiones es visualizable y descargable por cualquier Administrador.

### B.6 CU006 — Generación de Reportes de Prospección (PDF)

**Precondiciones — texto anterior.** – El Administrador debe haber iniciado sesión exitosamente. – Debe encontrarse en el módulo de reportes de prospección. – El documento del que se generará el reporte debe encontrarse en el sistema.

**Precondiciones — texto nuevo.** – El Administrador debe haber iniciado sesión exitosamente. – Debe encontrarse en el módulo de reportes de prospección. – La empresa y el año deben tener documentos elegibles (completados, con análisis y con la publicación vectorial confirmada) de una empresa activa. – Para generar o previsualizar, todas las filas GRI del análisis deben tener estado asignado y el análisis debe tener una validación vigente.

**Flujo Básico — texto anterior.** Generar reportes de prospección. 1. El Administrador accede al módulo de reportes de prospección. 2. Es necesario llenar los datos del sector, empresa y año del cual se generará dicho documento. 3. Tiene la capacidad de hacer una vista previa del documento previa a la generación y descarga del documento. 4. Puede generar y descargar el documento de reporte de prospección.

**Flujo Básico — texto nuevo.** Generar reportes de prospección. 1. El Administrador accede al módulo de reportes de prospección. 2. Es necesario llenar los datos del sector, empresa y año del cual se generará dicho documento. 3. Puede hacer una vista previa del documento: si la validación vigente ya tiene reporte, la vista previa es ese reporte; si no, es una vista previa candidata que no se guarda. 4. Puede generar y descargar el documento de reporte de prospección: cada validación vigente sin reporte da lugar a una versión nueva y una solicitud repetida para la misma validación devuelve la versión ya emitida. 5. Puede descargar el reporte actualizado o cualquier versión anterior por su identificador.

**Flujo Alternativo — texto anterior.** Generar reporte de un año que no haya documento: El sistema no habilita la vista previa, generación o descarga del documento ya que no hay posibilidad de hacerlo. Generar un reporte de un documento previamente usado: El sistema no permitirá generar un reporte de un documento del cual ya se haya hecho un reporte.

**Flujo Alternativo — texto nuevo.**
* Generar reporte de un año que no haya documento: El sistema no habilita la vista previa ni la generación, ya que no hay posibilidad de hacerlo; si el año tiene reportes anteriores, siguen disponibles para su descarga por identificador.
* Generar sin una validación nueva: El sistema no crea otra versión; devuelve la ya emitida para esa validación.
* Existe una validación posterior a la última versión emitida: El sistema informa que el reporte necesita actualización, no entrega como actualizado el PDF anterior y permite emitir la versión siguiente si el análisis tiene validación vigente; la versión anterior sigue descargable por su identificador.
* El análisis tiene cambios por revisar o validar: El sistema no habilita la emisión ni presenta el reporte anterior como actualizado, e indica si corresponde revisar o validar.
* El reporte es un histórico de origen no verificable: El sistema lo conserva y permite descargarlo por su identificador, no lo presenta como actualizado y, si el análisis tiene validación vigente, permite emitir una versión nueva.
* La validación de origen deja de ser vigente antes de confirmar: Si al realizar la comprobación final la validación de origen ya no es vigente, el sistema no confirma la nueva versión ni publica ningún reporte. Los cambios posteriores a la confirmación no modifican la emisión histórica.

**Post Condiciones — texto anterior.** – Éxito: El documento es generado, capaz de visualizarse y descargarse. – Fallo: El documento no es capaz de ser generado si el documento no existe.

**Post Condiciones — texto nuevo.** – Éxito: Se crea una versión inmutable del reporte, capaz de visualizarse y descargarse, o se devuelve la ya emitida para la misma validación; se conserva intacta cada versión anterior (PDF, snapshot y trazabilidad). – Fallo: No se crea ninguna versión si el análisis no tiene validación vigente, si al realizar la comprobación final la validación de origen ya no es vigente o si el PDF no pudo generarse. Los cambios posteriores a la confirmación no modifican la emisión histórica.

**Restricciones — texto anterior.** – Solo el rol Administrador puede acceder a este módulo. – El documento del cual se hará el reporte debe existir en el sistema. – No es posible generar un documento del mismo año, empresa y sector.

**Restricciones — texto nuevo.** – Solo el rol Administrador puede acceder a este módulo. – El análisis del cual se hará el reporte debe tener una validación vigente. – Una versión emitida no puede modificarse ni eliminarse. – Una versión nueva debe indicar la validación GRI que la respalda, de la misma empresa y año; los reportes históricos son la excepción. – No se emite otra versión para una validación que ya tiene reporte.

### B.7 Estados, definiciones y contratos

**Definiciones (glosario de reportes) — texto nuevo.**
* *Versión del reporte*: ordinal de la emisión de una empresa y un año (1, 2, …); cada una es inmutable.
* *Validación*: confirmación humana del análisis GRI de una empresa y un año; respalda como mucho un reporte.
* *Reporte actualizado*: la última versión emitida respalda la última validación registrada y esa validación sigue vigente respecto del análisis actual.
* *Actualización pendiente*: existe una validación posterior a la que respalda la última versión que aún no se emitió.
* *Cambios sin validar*: hay una validación registrada, pero el análisis actual ya no la hace vigente (estados, documentos o elegibilidad cambiaron).
* *Origen verificable*: se sabe qué validación respalda el reporte (por su vínculo o, en un histórico, por el número y la huella de documentos guardados en su snapshot).

**Estados de un año — texto nuevo.** `PENDIENTE_REVISION`, `PENDIENTE_VALIDACION`, `VALIDACION_VIGENTE`, `REPORTE_GENERADO` (solo con reporte actualizado), `ACTUALIZACION_PENDIENTE`, `SIN_CODIGOS_GRI`, `SIN_DOCUMENTACION_ELEGIBLE`. Se puede generar exactamente en `VALIDACION_VIGENTE` y `ACTUALIZACION_PENDIENTE`. El estado refleja el análisis actual; un reporte anterior queda como referencia.

**Contratos — texto nuevo.** Los de las secciones A.8 y A.10 de este informe (rutas, campos, códigos 201/200, errores 409 `REPORT_UPDATE_PENDING`, `REPORT_ORIGIN_UNVERIFIABLE`, `REPORT_ANALYSIS_NOT_CURRENT`, `REPORT_SOURCE_CHANGED`, `REPORT_VERSION_CONFLICT`, `ANALYSIS_NOT_VALIDATED`, y `accion_sugerida`).
