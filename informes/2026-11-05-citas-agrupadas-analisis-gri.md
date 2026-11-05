# Citas agrupadas en Análisis GRI (backend)

Estado: implementado en el backend y verificado localmente. La pantalla **no** está corregida hasta que el frontend consuma los campos nuevos (ver A.6).

## A. Solución aplicada

### A.1 Causa y qué hacía ya `pasajes.py`

Una tarjeta GRI repetía el mismo texto porque cada mención detectada es una evidencia y `pasajes.py` solo reúne evidencias **de la misma posición**: las que comparten línea (varias referencias en una línea) o cuyas ventanas se solapan, dentro del mismo documento y sección. Un texto idéntico que aparece en otra posición o sección es, por diseño, otro pasaje (lo fija el test `test_el_mismo_texto_en_pasajes_distintos_no_se_fusiona_por_parecido`). Por eso un párrafo repetido en varias secciones seguía mostrándose varias veces.

| Concepto | Qué es | Dónde |
|---|---|---|
| Mención | Aparición detectada de una referencia GRI. Es una `Evidencia`; `total_menciones` las cuenta. | `FilaGri.evidencias` |
| Pasaje | Menciones del mismo documento y sección que comparten línea o ventanas solapadas. Unidad posicional. | `FilaGri.pasajes` (sin cambios) |
| Cita agrupada | Pasajes de la misma fila y del mismo documento cuyo texto de presentación es **exactamente** igual, mostrados una vez. | `FilaGri.citas_agrupadas` (nuevo) |

`pasajes.py` no se modificó. La agrupación nueva parte de sus pasajes tal como salen, de modo que conserva su tratamiento de solapamientos y la trazabilidad a cada evidencia.

### A.2 Reglas

* Ámbito: una misma fila (identidad GRI: código y edición) y un mismo `documento_id`. Dos documentos con el mismo nombre no se fusionan; filas, estándares o ediciones distintos nunca comparten grupo porque la función trabaja sobre los pasajes de **una** fila.
* Igualdad exacta del texto completo del pasaje (`cita_legible`, ya limpio para presentación; sin truncar). Sin minúsculas, sin quitar puntuación, cifras, acentos ni códigos; sin embeddings ni IA. No hay reglas de limpieza nuevas: se usa la existente.
* Una mención se cuenta una sola vez: el índice de cada evidencia se registra en un conjunto y no se vuelve a sumar aunque cruce pasajes. Dos menciones legítimas con el mismo texto siguen existiendo (en `evidencias` y en los índices del grupo); solo se presentan juntas.
* Orden: el de primera aparición de cada grupo en `pasajes` (documentos por su primera mención y, dentro de cada uno, por posición). Es determinista.
* Cálculo solo para presentación al responder: sin migraciones, sin escrituras y sin reingesta. Con `presentacion=False` (las escrituras) no se calcula y los campos nuevos quedan vacíos.
* Coste: una pasada con un diccionario por (documento, texto), sin comparar cada cita con las demás. Tiempo esperado O(P + L + A): P pasajes, A apariciones (menciones), que se recorren una vez para contarlas y guardar sus índices, y L la longitud total de los textos de los pasajes (cada uno se hashea una vez). Memoria adicional: los grupos con sus referencias distintas, secciones e índices de evidencia y de pasaje (proporcional a G + A + P, con G ≤ P grupos) más el conjunto de menciones ya contadas (A); los textos son los mismos objetos de los pasajes, no copias.

### A.3 Contrato (aditivo)

En cada `FilaGri` de `GET /analisis-gri/empresas/{empresa_id}/anios/{anio}`:

| Campo | Significado |
|---|---|
| `citas_agrupadas` | **Lista que el frontend debe renderizar.** Un elemento por texto idéntico y documento. |
| `total_citas_agrupadas` | Cantidad de citas agrupadas. Es independiente de `total_menciones` («54 menciones · 20 citas únicas»). |

Cada elemento (`CitaAgrupada`):

| Campo | Significado |
|---|---|
| `documento_id`, `documento_nombre` | Documento de origen; el nombre es el del archivo (`nombre.md`). |
| `cita_legible` | Texto completo a mostrar. |
| `total_menciones` | Menciones originales que agrupa (cada una cuenta una vez). |
| `referencias` | Referencias originales sin repetidos, en orden de aparición (p. ej. `GRI 3-1`, `GRI 3-2`). |
| `ubicaciones` | Todas las secciones donde aparece el texto (`seccion_legible`, `total_menciones`, `indices_evidencia`). |
| `indices_evidencia` | Posiciones de sus menciones en `FilaGri.evidencias` (que conserva las posiciones originales). |
| `indices_pasaje` | Posiciones de los pasajes que reúne en `FilaGri.pasajes`. |

Ausencia de sección: `seccion_legible` es la cadena vacía `""`, de forma consistente; el frontend debe mostrar «Sin sección identificada».

Los datos son estructurados (sin HTML ni etiquetas). No cambia ningún campo existente: `total_menciones`, `menciones_en_indice`, `menciones_en_cuerpo`, `evidencias`, `pasajes`, identidades, estados, puntaje ESG, `version` y `huella_documentos` se calculan igual. El esquema OpenAPI añade `CitaAgrupada` y `UbicacionCita`.

Ejemplos reales (producidos por el código con los detectores de la ingesta; los identificadores se sustituyeron por valores de ejemplo y se recortó cada fila a los campos nuevos):

1. Una cita con una mención.

```json
{"identidad": "204:2016", "total_menciones": 1, "total_citas_agrupadas": 1, "citas_agrupadas": [
  {"documento_id": "00000000-0000-4000-8000-000000000001", "documento_nombre": "memoria.md",
   "cita_legible": "Cumplimos GRI 204-1 con proveedores locales.", "total_menciones": 1, "referencias": ["GRI 204-1"],
   "ubicaciones": [{"seccion_legible": "Informe > Compras", "total_menciones": 1, "indices_evidencia": [0]}],
   "indices_evidencia": [0], "indices_pasaje": [0]}]}
```

2. Varias menciones en una sección (el mismo párrafo dos veces en «Compras»).

```json
{"identidad": "204:2016", "total_menciones": 2, "total_citas_agrupadas": 1, "citas_agrupadas": [
  {"documento_id": "00000000-0000-4000-8000-000000000001", "documento_nombre": "memoria.md",
   "cita_legible": "Cumplimos GRI 204-1 con proveedores locales.", "total_menciones": 2, "referencias": ["GRI 204-1"],
   "ubicaciones": [{"seccion_legible": "Informe > Compras", "total_menciones": 2, "indices_evidencia": [0, 1]}],
   "indices_evidencia": [0, 1], "indices_pasaje": [0, 1]}]}
```

3. Una cita idéntica en varias secciones del mismo documento (todas las ubicaciones se conservan).

```json
{"identidad": "204:2016", "total_menciones": 2, "total_citas_agrupadas": 1, "citas_agrupadas": [
  {"documento_id": "00000000-0000-4000-8000-000000000001", "documento_nombre": "memoria.md",
   "cita_legible": "Cumplimos GRI 204-1 con proveedores locales.", "total_menciones": 2, "referencias": ["GRI 204-1"],
   "ubicaciones": [
     {"seccion_legible": "Informe > Compras", "total_menciones": 1, "indices_evidencia": [0]},
     {"seccion_legible": "Informe > Contratos", "total_menciones": 1, "indices_evidencia": [1]}],
   "indices_evidencia": [0, 1], "indices_pasaje": [0, 1]}]}
```

4. Texto idéntico en documentos diferentes: grupos separados.

```json
{"identidad": "204:2016", "total_menciones": 2, "total_citas_agrupadas": 2, "citas_agrupadas": [
  {"documento_id": "00000000-0000-4000-8000-000000000001", "documento_nombre": "memoria-2025.md",
   "cita_legible": "Cumplimos GRI 204-1 con proveedores locales.", "total_menciones": 1, "referencias": ["GRI 204-1"],
   "ubicaciones": [{"seccion_legible": "Informe > Compras", "total_menciones": 1, "indices_evidencia": [0]}],
   "indices_evidencia": [0], "indices_pasaje": [0]},
  {"documento_id": "00000000-0000-4000-8000-000000000002", "documento_nombre": "reporte-gri-2025.md",
   "cita_legible": "Cumplimos GRI 204-1 con proveedores locales.", "total_menciones": 1, "referencias": ["GRI 204-1"],
   "ubicaciones": [{"seccion_legible": "Informe > Compras", "total_menciones": 1, "indices_evidencia": [1]}],
   "indices_evidencia": [1], "indices_pasaje": [1]}]}
```

5. Sin sección: la ubicación trae `"seccion_legible": ""`.

### A.4 Consumidores y reportes

* El reporte usa el primer pasaje de cada fila (`pasajes[0]`); no consume los campos nuevos, así que su HTML no cambia.
* El snapshot guarda `AnalisisResponse` completo. Para que el snapshot de una emisión nueva conserve exactamente la forma de las anteriores, la emisión excluye los dos campos nuevos (`SNAPSHOT_SIN_CITAS_AGRUPADAS` en `app/modules/reportes/service.py`). Los snapshots, PDFs y reportes ya emitidos no se tocan. La vigencia (`version` de estados y `huella_documentos`), el versionado y sus protecciones no se modificaron.

### A.5 Archivos

* `app/modules/analisis_gri/domain/citas_agrupadas.py` (nuevo): función pura `agrupar_citas`.
* `app/modules/analisis_gri/schemas.py`: `UbicacionCita`, `CitaAgrupada` y los campos de `FilaGri`.
* `app/modules/analisis_gri/domain/calculo.py`: la fila se completa con las citas agrupadas.
* `app/modules/reportes/service.py`: exclusión de los campos nuevos en el snapshot.
* `pruebas-zap/pruebas/fixtures/openapi-real.json`: solo los esquemas `FilaGri`, `CitaAgrupada` y `UbicacionCita`.
* Pruebas: `tests/services/test_analisis_gri_citas_agrupadas.py` y `tests/services/test_analisis_gri_citas_agrupadas_postgres.py`.

### A.6 Guía para frontend

1. **Qué lista renderizar:** `fila.citas_agrupadas`, no `evidencias` ni `pasajes` (siguen disponibles para el detalle).
2. **Contador:** `cita.total_menciones` («2 menciones»; con 1, «1 mención»). Las referencias se muestran con `cita.referencias`, p. ej. `(GRI 3-1, GRI 3-2) · 2 menciones`.
3. **Documento y Sección:** `Documento: {cita.documento_nombre}` y `Sección: {ubicacion.seccion_legible}`. Si `seccion_legible` es `""`, mostrar «Sin sección identificada».
4. **Varias ubicaciones:** si `cita.ubicaciones.length > 1`, mostrar la primera y un enlace «Ver ubicaciones» que despliegue todas (cada una con su sección y `total_menciones`). No atribuir el texto solo a la primera.
5. **«Ver N citas más»:** `N = fila.total_citas_agrupadas − citasMostradas`. No usar `total_menciones`. Un resumen como «54 menciones · 20 citas únicas» sale de `total_menciones` y `total_citas_agrupadas`.
6. Para ver la posición original de una mención, usar `indices_evidencia` sobre `fila.evidencias` (`cita_inicio`, `cita_fin`, `inicio`, `fin`).

### A.7 Limitaciones

* La presentación visual queda pendiente de integración en el frontend.
* La agrupación es por texto exacto: variantes de cifras, puntuación, mayúsculas o espacios no se unen (es lo pedido).
* Dos secciones cuyo texto legible coincide se muestran como una ubicación (los índices distinguen las menciones).
* La respuesta crece con `citas_agrupadas` (repite el texto de los pasajes); `pasajes` y `evidencias` se conservan por compatibilidad.

### A.8 Verificación realizada y pendientes

Ejecutado localmente sobre el código definitivo (dependencias declaradas del proyecto y PostgreSQL 17.4 aislado y temporal):

* Sintaxis y `pyflakes` de los archivos del alcance: sin hallazgos. `git diff --check`: sin espacios finales en los archivos de esta funcionalidad.
* Pruebas de citas agrupadas (24 de dominio y 6 con PostgreSQL), `test_analisis_gri_citas_pasajes.py` y `test_analisis_gri_citas_legibles_postgres.py`: 52 pasaron, ninguna omitida.
* Snapshot, contrato OpenAPI y consumidores de reportes (`test_gestor_unidades.py`, `test_main.py`, `test_alcance_zap.py`, `test_rutas_estaticas.py`, `test_reportes_postgres.py`, `test_reportes_versionado_postgres.py`, `test_reportes_service.py`, `test_analisis_gri_postgres.py`): 131 pasaron y 1 se omitió (la prueba que compila un PDF real con WeasyPrint, que no carga en Windows).
* Fixture de ZAP: `unidad.test.mjs`, 32 de 32.
* La prueba de conservación (datos originales y huella de documentos) se comprobó de forma aislada y en memoria: detecta tanto la alteración del análisis original como la del `sha256` de un documento (la huella).

No ejecutado: Jenkins, Sonar (la revisión local no demuestra el Quality Gate), ZAP, QA/UAT, pgvector y MinIO reales, ni la imagen Docker. Pendiente de integración: el frontend (la pantalla no se corrige hasta que consuma los campos nuevos).

## B. Cambios en Análisis y Diseño

El documento oficial no está disponible para editarlo; estas sustituciones son para incorporarlo. «Texto anterior» es el de las fuentes locales (`docs/fuentes/latex`, históricas y de numeración distinta). No hay cambios en el modelo de datos: la agrupación se calcula al responder y no se persiste.

### B.1 RF-028 (tabla de requisitos)

**Texto anterior.** Visualizar las brechas GRI de una empresa con su cita de respaldo.

**Texto nuevo.** Visualizar las brechas GRI de una empresa con su cita de respaldo. Cada cita idéntica del mismo documento se presenta una sola vez, con el número de menciones que agrupa y todas sus ubicaciones (documento y sección).

### B.2 CU005, Flujo Básico, paso 2

**Texto anterior.** El sistema muestra los códigos GRI detectados con su cita textual (documento y sección) y las sanciones identificadas.

**Texto nuevo.** El sistema muestra los códigos GRI detectados con su cita textual y las sanciones identificadas. Cuando una misma cita aparece varias veces en una fila GRI y un documento, el sistema la muestra una sola vez con el número de menciones y todas sus ubicaciones (documento y sección); si una mención no tiene sección, se indica «Sin sección identificada». Los textos idénticos de documentos distintos se muestran por separado. Las menciones originales no se eliminan ni cambian los conteos, los estados ni el puntaje.

### B.3 CU005, Restricciones

**Texto anterior.** Toda brecha y sanción reportada debe citar su fuente (documento y sección).

**Texto nuevo.** Toda brecha y sanción reportada debe citar su fuente (documento y sección). La agrupación de citas idénticas es solo de presentación: compara el texto exacto, dentro de la misma fila GRI y el mismo documento, sin usar similitud semántica ni modelos de IA.

### B.4 Glosario

**Texto nuevo.** *Cita agrupada*: texto idéntico de una misma fila GRI y un mismo documento, presentado una sola vez con el número de menciones que reúne y sus ubicaciones. *Mención*: aparición detectada de una referencia GRI en un documento. *Cita única*: cita agrupada; su cantidad es distinta de la de menciones.
