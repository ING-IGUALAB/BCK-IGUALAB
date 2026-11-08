# Sector «Otros» en el catálogo de sectores de las empresas

Fecha: 2026-11-05. Estado: implementado y verificado localmente; la migración 0008 **no** se aplicó a QA, UAT ni producción. Verificación desplegada pendiente (ver «Pendiente»).

## Decisión de negocio confirmada

«Otros» es una **cuarta opción fija** del catálogo de sectores:

| Valor API y PostgreSQL | Etiqueta del frontend |
|---|---|
| `OTROS` | Otros |

Las empresas de este sector usan **todo el flujo** bajo las mismas reglas y permisos que las demás: administración de empresas, ingesta, análisis y validación GRI, generación y descarga de reportes, asistente IA (chat RAG) y consultas y filtros por sector. **No hay exclusión de elegibilidad por sector.** Siguen aplicándose las reglas generales: empresa activa, documentos elegibles, validación vigente, permisos, separación por empresa, año y ambiente, y la prohibición de cambiar el sector de una empresa que ya tiene documentos. No hay texto libre, descripción ni catálogo administrable.

Los cuatro sectores definidos en el código (`SectorEmpresa`, `app/models/empresas.py`): `MINERIA`, `PETROLEO`, `ENERGIA`, `OTROS`.

## Lo aplicado

- `SectorEmpresa.OTROS`. Los valores anteriores no se renombran ni cambian y `OTROS` no es predeterminado (`MetadatosIngestaRequest.sector` sigue siendo opcional y nulo por omisión).
- Un único tipo enumerado de PostgreSQL, `sector_empresa`, lo usan `empresas.sector` y `documentos.sector` (la copia de la ingesta). La base vectorial guarda el sector como `VARCHAR(64)` con solo `CHECK (length(trim(sector)) > 0)`: no necesita migración.
- **Migración transaccional 0008** (`app/migraciones/transaccional/0008_sector_otros.sql`): `ALTER TYPE sector_empresa ADD VALUE IF NOT EXISTS 'OTROS'`. Registrada en `app/migraciones/catalogo.py` (versión 8, sin `objetos`, como la 0005–0007) y aplicada por el arranque una sola vez. Idempotente: una instalación nueva ya trae el valor por `create_all`. No toca filas ni restricciones, no recrea tablas y no modifica las migraciones 0001–0007 ni sus checksums; `firmas.json` no cambia (los tipos no forman parte de la firma).
- **Compatibilidad en cada arranque** (`app/migraciones/compatibilidad.py`): exige todos los miembros de `SectorEmpresa` en `sector_empresa`; con el tipo anterior el arranque falla con `CENTRAL_SCHEMA_INCOMPATIBLE` (`valor_de_tipo:sector_empresa.OTROS`).
- **Contrato OpenAPI:** `pruebas-zap/pruebas/fixtures/openapi-real.json` actualizado solo en el enum `SectorEmpresa` (`+8 bytes`). Se comparó antes y después con `app.openapi()`: había exactamente una diferencia y ahora hay cero; rutas y demás esquemas no cambiaron.
- No se tocó ninguna regla de negocio: ningún código de `app/` distingue sectores concretos (se buscó `SectorEmpresa.MINERIA/PETROLEO/ENERGIA` fuera del modelo y no hay listas de sectores fijadas a mano).
- Ayudante de pruebas: `crear_empresa` (`tests/ayudantes_analisis_gri.py`) acepta `sector` (por omisión sigue siendo `MINERIA`).

## Evidencia de que `OTROS` participa en cada módulo

| Módulo | Cómo participa (reglas actuales, sin ramas especiales) | Prueba |
|---|---|---|
| Empresas | El enum de `CrearEmpresaRequest`/`EditarEmpresaRequest`, el filtro `?sector=` y `listar_empresas` usan `SectorEmpresa`. La regla de sector bloqueado con documentos se aplica igual. | `tests/routers/test_empresas.py` (crear, editar, consultar, filtrar, mantener los demás, rechazar `OTRO`/`Otros`/`otros`, regla con documentos); `test_sector_otros_postgres.py` (servicio sobre PostgreSQL) |
| Ingesta | El sector sale de la empresa (`obtener_empresa_activa`); el declarado solo se contrasta (`COMPANY_SECTOR_MISMATCH`). La reserva copia `empresa.sector` a `documentos.sector`. | `test_ingesta_validacion.py`; `test_sector_otros_postgres.py` (reserva real, copia y rechazo sin crear documento) |
| Almacenamiento y filtro vectorial | `_texto` convierte el miembro a su valor; se guarda en `fragmentos_documento.sector` y el filtro es `sector = :sector`. | `test_sector_otros_vectorial.py` (sesión de captura; los 4 sectores). La ejecución contra pgvector real está en `test_fragmentos_vectoriales.py` y **no se ejecutó** (Docker apagado) |
| Análisis y validación GRI | `consultar_analisis` lee la empresa y su sector solo para mostrarlo; no filtra por sector. | `test_sector_otros_flujo_postgres.py`: mismas filas que una empresa de control con el mismo documento |
| Reportes | Disponibilidad, emisión y descarga dependen de empresa activa, documentos elegibles y validación vigente. El PDF muestra el identificador del sector, como con los demás (`Sector: OTROS`). | `test_sector_otros_flujo_postgres.py`: validación, emisión (versión 1), vigente, descarga y disponibilidad iguales a las del control; sin validación se rechaza igual que al control |
| Asistente IA | `_empresa_consultable` contrasta empresa, sector y estado; la búsqueda se limita a ambiente, empresa, año y sector. | `test_sector_otros_asistente_postgres.py` con dobles controlados de embeddings, modelo de chat y búsqueda (sin OCI ni DeepSeek) |

## Pruebas y resultados (locales)

- Sin PostgreSQL: **377 pasaron, 30 omitidas** (todas por Docker/pgvector) — empresas HTTP, validación de ingesta, vectorial con captura, motor de migraciones simulado, arranque del Módulo 5, alcance ZAP, firmas.
- PostgreSQL 17.4 aislado (clúster temporal): **80 pasaron, 0 omitidas** — migración 0008, flujo de reportes, asistente, empresa/sector, estado de reportes.
- Migraciones y esquema central (PostgreSQL aislado): un lote de 8 archivos (migraciones, `not_null`, esquema central, auditoría, análisis GRI, integridad y migración de reportes, empresa/sector) dio 130 pasaron, 16 omitidas y 1 fallo por una aserción con la lista de nombres de migraciones, que se corrigió; después `test_migraciones.py` se repitió completo (34 pasaron, 9 omitidas) y `test_migraciones_not_null.py` se repitió con el detalle por variante (20 pasaron, 7 omitidas). Esos archivos no cambiaron después de esas ejecuciones.
- `pruebas-zap/scripts/pruebas/unidad.test.mjs` y `ejecutor.test.mjs` (Node 22.14): **63 pasaron**, y `tests/test_alcance_zap.py` pasó. Son pruebas del alcance y del contrato; **no** son un escaneo ZAP real.
- Migración 0008 sobre el esquema anterior con datos (PostgreSQL aislado): antes de aplicarla `OTROS` se rechaza y el arranque falla; después se aplican solo la versión 8, empresas y documentos quedan idénticos, los valores anteriores no cambian, `OTROS` se acepta en `empresas` y `documentos`, no hay valor predeterminado, el arranque siguiente no ejecuta nada y los checksums anteriores se verifican sin error. Instalación nueva: el valor ya existe y la 0008 no falla.
- Se actualizaron las aserciones que fijaban la lista de migraciones (1–7 → 1–8); no se eliminó ni relajó ninguna prueba.

### Omitidas (no declaradas aprobadas)

Docker Desktop está detenido y el equipo tiene poca memoria libre; no se arrancó. Quedan sin ejecutar **16 pruebas con contenedor**: 9 vectoriales de `test_migraciones.py` (pgvector real; no modificadas) y 7 variantes `[contenedor]` de `test_migraciones_not_null.py`. Las aserciones que se modificaron en esas 7 se ejecutaron y pasaron en las variantes `[local]` equivalentes (PostgreSQL 17.4); no se ejecutaron en PostgreSQL 16/18 de Docker. Tampoco se ejecutaron `test_fragmentos_vectoriales.py` ni las firmas con pgvector real.

## Nota de integración para el frontend (pendiente; no hay proyecto frontend en este entorno)

El selector visual **no** se actualizó ni se verificó. Pendiente:

1. Añadir la etiqueta «Otros» y enviar el valor `OTROS` (no la etiqueta: la API responde 422 a «Otros»).
2. Revisar los selectores y filtros de sector de empresas (crear, editar, listado), ingesta y asistente; si el frontend duplica la lista de sectores, debe incluir `OTROS`.
3. Si algún componente asume tres sectores (iconos, colores, textos), cubrir el cuarto.

## Sustituciones para Análisis y Diseño (no se modificó ninguna fuente)

Las fuentes disponibles son históricas o ajenas al repositorio: `docs/` está ignorado por git y los documentos de Análisis y Diseño viven fuera del repositorio (`igualab_md/01-Mockups-y-Propuestas`, en LaTeX y en Markdown, con numeraciones distintas: la regla de sectores es RN-015 en el LaTeX y RN-019 en el Markdown). **No se editó ninguno**; las sustituciones exactas son estas. Los nombres de los cuatro sectores en el código son `MINERIA`, `PETROLEO`, `ENERGIA` y `OTROS`; las etiquetas usadas hasta ahora son «Minería», «Petróleo y Gas» y «Energía» (el Markdown también dice «Petróleo y Energía» y «petrolera»: unificar), más «Otros».

Se resuelve así la discrepancia D08 (alcance): `OTROS` es elegible. La etiqueta de `PETROLEO` («Petróleo y Gas») no cambia.

**LaTeX (`analisis-diseno-latex`)**

1. `glosario.tex:21`, «Sectores habilitados».
   - Actual: «Los tres sectores acordados con el cliente para los cuales el sistema ejecuta el análisis de brechas: Minería, Petróleo y Gas, y Energía.»
   - Nuevo: «Las cuatro opciones fijas del catálogo de sectores de una empresa: Minería, Petróleo y Gas, Energía y Otros (identificadores MINERIA, PETROLEO, ENERGIA y OTROS). “Otros” es una opción más, sin texto libre ni descripción; todas siguen las mismas reglas del análisis de brechas.»
2. `rn.tex:33`, RN-015 «Sectores habilitados (conjunto cerrado)».
   - Actual: «…ENTONCES el sistema solo admite tres sectores: Minería, Petróleo y Gas, y Energía. … Y TIENE LA SIGUIENTE EXCEPCIÓN no se crean sectores nuevos; las empresas de otros sectores pueden registrarse, pero no son elegibles para el análisis ni para la generación de reportes.»
   - Nuevo: «…ENTONCES el sistema solo admite cuatro sectores: Minería, Petróleo y Gas, Energía y Otros. … SE BASA EN el acuerdo con el cliente y en la decisión de negocio que añade “Otros” como cuarta opción fija (registrar el acta correspondiente). Y TIENE LA SIGUIENTE EXCEPCIÓN no se crean más sectores, no se admite texto libre ni descripción y no existe un catálogo administrable; ninguna empresa queda excluida del análisis ni de los reportes por su sector, y “Otros” sigue las mismas reglas generales (empresa activa, documentos elegibles, validación vigente y permisos).»
3. `s01-06.tex:86` (registro de empresas).
   - Actual: «…pertenece a un conjunto cerrado de tres valores (Minería, Petróleo y Gas, y Energía); las empresas de otros sectores no son elegibles para el análisis ni para la generación de reportes.»
   - Nuevo: «…pertenece a un catálogo cerrado de cuatro opciones fijas (Minería, Petróleo y Gas, Energía y Otros), sin texto libre ni catálogo administrable; todas las empresas, cualquiera sea su sector, son elegibles para el análisis y la generación de reportes bajo las mismas reglas generales.»
4. `s08-13.tex:95` y `127` (sectores en la base de datos).
   - Actual: «CHECK sectores cerrados en empresas.sector & Minería · Petróleo y Gas · Energía (RN-015, en la BD).» y «8.7.1. Sectores cerrados: Minería, Petróleo y Gas, Energía (CHECK en la BD).»
   - Nuevo: «Enumerado sector\_empresa (empresas.sector y documentos.sector) & MINERIA · PETROLEO · ENERGIA · OTROS (RN-015).» y «8.7.1. Sectores cerrados: Minería, Petróleo y Gas, Energía y Otros (tipo enumerado `sector_empresa` de PostgreSQL, no un CHECK; la base vectorial guarda el sector como VARCHAR(64) sin lista de valores).»
5. `s08-13.tex:99`: la fila «CHECK estados ENUM» enumera `estado_ingesta`, `estado_gri`, `estado_documental`, `tipo_evento_auditoria`; añadir `sector_empresa` a los enumerados y no a los CHECK.

**Markdown (`Documento_de_Analisis_y_Diseno_Igualab.md`)**

6. Línea 90 (módulo de empresas): sustituir «El análisis de brechas se limita a los sectores acordados con el cliente: Minería, Petróleo y Energía; empresas de otros sectores pueden registrarse en el catálogo, pero no son elegibles para el análisis ni para la generación de reportes.» por «El sector pertenece a un catálogo cerrado de cuatro opciones fijas: Minería, Petróleo y Gas, Energía y Otros, sin texto libre ni catálogo administrable; todas las empresas son elegibles para el análisis y la generación de reportes bajo las mismas reglas generales.»
7. Línea 130, RN-019 «Acotación de Sectores del Análisis»: sustituir «El análisis de brechas GRI y sanciones se limita a empresas de los sectores Minería, Petróleo y Gas, y Energía, acordados con el cliente.» por «El análisis de brechas GRI y sanciones se aplica a empresas de los cuatro sectores del catálogo (Minería, Petróleo y Gas, Energía y Otros); no se excluye a ninguna empresa por su sector.» RN-017 («…los sectores habilitados») no requiere cambio de texto.
8. Línea 754, RF-053: sustituir «asignarlo a un sector (minería, petrolera o energía)» por «asignarlo a uno de los cuatro sectores fijos del catálogo (Minería, Petróleo y Gas, Energía u Otros)».

## Pendiente

- Frontend: etiqueta, valor y selectores (arriba).
- Verificación desplegada: Jenkins, Sonar, QA/UAT y la imagen del backend; aplicar la migración 0008 en cada ambiente (la aplica el arranque, no se ejecutó en ninguna base compartida); pruebas con pgvector y PostgreSQL en contenedor; un escaneo ZAP real.
- Sin implementar (tarea aparte): la confirmación de documentos observados.
