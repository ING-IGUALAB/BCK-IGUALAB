# Pruebas HTTP del backend: integración y E2E

Colecciones **Postman** (formato v2.1) ejecutadas con **Newman 6.2.1** sobre **Node 22.14.0**, contra un backend **ya desplegado**
(sin mocks). Prueban el contrato real (códigos de error de `app/exceptions.py`, esquemas de `app/schemas*.py`, OpenAPI), no solo el HTTP 200.
No sustituyen las pruebas pytest (`tests/`) ni las E2E de interfaz del frontend.

| Pieza | Versión fijada en | Por qué |
|---|---|---|
| Newman | `package.json` (`6.2.1`) + `package-lock.json` | Se instala con `npm ci`: mismas dependencias en cada corrida. |
| Node | `Dockerfile` (`node:22.14.0-bookworm-slim` **con digest**) | Jenkins no necesita Node ni Newman instalados; solo Docker, que ya usa. |

## Contenido

```
pruebas-http/
├─ colecciones/
│  ├─ igualab-integracion.postman_collection.json   158 peticiones, 377 aserciones
│  ├─ igualab-e2e.postman_collection.json            50 peticiones: los 4 flujos (A, B, C, D)
│  └─ fuentes/                                       código que GENERA los JSON (DSL legible); editar aquí
├─ scripts/ejecutar.mjs (+ lib/)                     ejecutor: valida, espera el backend, prepara muestras, corre Newman, informa
├─ scripts/ejecutar-local.ps1                        atajo para el entorno local aislado (Windows)
├─ muestras/                                         plantilla sintética GRI y archivos fijos para los rechazos
├─ entorno/ejemplo.env, ejemplo.postman_environment.json   solo NOMBRES de variables, sin valores
├─ Dockerfile, package*.json
```

Los JSON están versionados porque son lo que se ejecuta; se **generan** desde `colecciones/fuentes/`:
`npm run generar` los reescribe y `npm run verificar-colecciones` falla si están desactualizados.

### Colección de integración (`--coleccion integracion`)
Autenticación, usuarios, empresas, documentos y análisis GRI: éxito con estructura y valores, 401 sin sesión, 403 por rol, 422 de validación,
404 y 409 representativos. **No ingiere documentos ni usa OCI**: solo crea cuentas y empresas sintéticas y prueba rechazos que ocurren
antes de cualquier servicio externo. **Nunca transfiere el rol SuperAdmin** (solo prueba los rechazos de la transferencia).

### Colección E2E (`--coleccion e2e`): los cuatro flujos confirmados
| Flujo | Pasos | Qué demuestra |
|---|---|---|
| **A** Administración y revocación de acceso | SuperAdmin → crea Administrador → éste inicia sesión y accede → SuperAdmin lo deshabilita → **la misma petición con el token anterior da 401** | La revocación es inmediata. Solo usa la cuenta que crea; no transfiere el rol. |
| **B** Ingesta y validación GRI | Empresa activa → operación → ingesta de un Markdown sintético (**OCI real**, muestra mínima) → COMPLETADO + publicado → Administrador consulta el análisis → asigna estado a cada identidad devuelta → consulta versión y huella actuales → valida | Puntaje esperado calculado desde los estados **realmente asignados**; validación vigente; historial; citas y procedencia exactas (`texto[inicio:fin]`). No usa sanciones: **no depende de CP37**. |
| **C** Duplicado por contenido | Mismos bytes que B, otro nombre de archivo y **otro año** → `409 DOCUMENT_ALREADY_INGESTED` con `details.criterio = ["sha256"]` | El original sigue COMPLETADO y disponible; no hay otro documento; la operación queda `RECHAZADA`; no es una ingesta exitosa. |
| **D** Empresa inactiva | Empresa → desactivarla → ingerir un Markdown válido y exclusivo → `400 COMPANY_INACTIVE` | La empresa sigue inactiva; no hay documento ni análisis utilizable; la operación informa el rechazo. |

**B prepara el documento que usa C.** Para ejecutarlos sin depender de corridas anteriores: `--carpeta B --carpeta C` (en ese orden). **A y D
preparan sus propios datos** y se pueden ejecutar solos. `--carpeta` acepta la letra («B») o el nombre completo.

> **Evidencia solo por HTTP.** Los contratos no permiten demostrar la ausencia *física* de objetos en MinIO ni de vectores en pgvector para un
> documento rechazado. C y D demuestran que **no hay documento, no hay análisis y la operación está RECHAZADA**; **no afirman** haber
> verificado el almacenamiento físico, y no hay (ni se añadieron) endpoints internos para ello.

## Credenciales y variables

Si falta algo el ejecutor **falla** (código 2) nombrando solo la variable; nunca hay una ejecución verde por falta de configuración.

| Variable | Qué es |
|---|---|
| `IGUALAB_API_URL` | URL base del backend, sin barra final. |
| `IGUALAB_E2E_SUPERADMIN_CORREO` | Correo del SuperAdmin del entorno. |
| `IGUALAB_E2E_SUPERADMIN_PASSWORD` | Contraseña **vigente** de ese SuperAdmin. No se asume que sea la de `SUPERADMIN_PASSWORD` del aprovisionamiento. |
| `IGUALAB_PRUEBAS_ENTORNO` (o `--entorno`) | `local`, `qa` o `uat`. Sin valor por defecto. |
| `IGUALAB_ESPERA_SEGUNDOS` (opc.) | Plazo máximo para que `/health` responda (por defecto 240). |

**Jenkins (QA):** credencial **`IGUALAB_QA_SUPERADMIN`** de tipo *Username with password* (usuario = correo, contraseña = la vigente). Opcional: variable
de entorno **`IGUALAB_QA_API_URL`** si el backend no se alcanza por el nombre de su contenedor
(por defecto `http://igualab-backend-qa-backend-1:8000` dentro de la red `proxy_net`).

Las contraseñas de las cuentas **sintéticas** se generan al azar en cada corrida (`Aa1!` + 24 caracteres aleatorios), solo existen en la memoria del
proceso y nunca se escriben. El SuperAdmin no se bloquea por un error de configuración: ante un 401/423 en su login la corrida **se detiene tras el primer
intento** (el backend bloquea la cuenta tras 5 intentos fallidos, RNF-031).

## Cómo ejecutarlas

```bash
# Instalar (una vez) y regenerar/verificar las colecciones
cd pruebas-http && npm ci && npm run verificar-colecciones

# Con Node local (no necesita Docker); las variables se pasan por entorno, nunca por argumentos
export IGUALAB_PRUEBAS_ENTORNO=local IGUALAB_API_URL=http://127.0.0.1:8000
export IGUALAB_E2E_SUPERADMIN_CORREO=...      # y la contraseña con:  read -rs IGUALAB_E2E_SUPERADMIN_PASSWORD; export IGUALAB_E2E_SUPERADMIN_PASSWORD
node scripts/ejecutar.mjs --coleccion integracion
node scripts/ejecutar.mjs --coleccion e2e --carpeta A --carpeta D         # sin OCI
node scripts/ejecutar.mjs --coleccion e2e --carpeta B --carpeta C         # usa OCI real
node scripts/ejecutar.mjs --coleccion e2e                                 # A, B, C y D
node scripts/ejecutar.mjs --coleccion e2e --sin-ejecutar                  # solo valida configuración, disponibilidad y muestras
```

Mismo contenedor que usa Jenkins (entorno local aislado, Windows):
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\pruebas-http\scripts\ejecutar-local.ps1 -Coleccion integracion
powershell -NoProfile -ExecutionPolicy Bypass -File .\pruebas-http\scripts\ejecutar-local.ps1 -Coleccion e2e -Carpeta A,D
```

Códigos de salida: `0` aprobado · `1` pruebas fallidas · `2` configuración · `3` backend no disponible · `4` guarda (cero pruebas, peticiones
omitidas, UAT) · `5` secreto hallado en los reportes. Una corrida solo es **APROBADA** si ejecutó **todas** las peticiones previstas, con al menos una
aserción por petición, sin fallos y con respuesta HTTP en cada una.

## Reportes (`resultados/<coleccion>/`)

`junit.xml` (Jenkins lo publica), `resumen.json`, `resumen.md` y `consola.log`. Se generan **solo** con campos permitidos: nombre de la petición,
método, **ruta sin consulta**, estado HTTP, tiempo y resultado de las aserciones. **Sin cabeceras, cuerpos, tokens ni contraseñas**: no se usa el reporter JSON
de Newman ni su salida de consola; además, al terminar se buscan en los archivos las contraseñas de la corrida y cualquier cadena con forma de JWT o
`Bearer …` (si aparece, falla con código 5). `resumen.json` incluye `datos_sinteticos_creados` (IDs y correos sintéticos, útiles para auditar).
Las colecciones no escriben en consola ni comparan tokens/contraseñas en aserciones (un fallo de chai mostraría el valor).

## Jenkins

`Jenkinsfile`: tras «Deploy QA (Docker Compose)», dos stages **solo en la rama `qa`**: **Integration Tests** y **E2E Tests**. Cada uno construye la imagen
`pruebas-http/Dockerfile`, la ejecuta en la red `proxy_net`, espera a `/health` con un plazo acotado, ejecuta, y en `post { always }` **archiva**
`pruebas-http/resultados/<coleccion>/**` y **publica `junit.xml`** (`allowEmptyResults: false`) aunque haya fallos. Un fallo de las pruebas hace fallar
el stage y el pipeline; no hay `|| true`. Además `buildDiscarder(logRotator(numToKeepStr: '1', artifactNumToKeepStr: '1'))`.
Se conservan las pruebas pytest, Sonar, el Quality Gate, los despliegues y la limpieza de `.env`.

## Datos que quedan y cómo limitar su acumulación

No hay endpoints de borrado ni se usa SQL. Cada corrida completa deja, **por diseño y con el prefijo `E2E <run_id>`**:

| Colección | Cuentas | Empresas | Documentos |
|---|---|---|---|
| Integración | 2 Administradores (se **deshabilitan** al final) | 2 (se **desactivan** al final) | 0 (deja 6 operaciones `RECHAZADA`/`CREADA` sin documento) |
| E2E (A+B+C+D) | 3 Administradores (A, B y D; los tres terminan deshabilitados) | 2 (B se desactiva al final de C; D nace inactiva) | **1 COMPLETADO** (flujo B), con sus fragmentos en pgvector y su original en MinIO; más 2 operaciones `RECHAZADA` (C y D) |

Para limitarlo: solo corre en `qa`; `disableConcurrentBuilds`; cada corrida usa una muestra mínima (≈ 450 bytes, unos pocos fragmentos); las cuentas y empresas
quedan inertes (deshabilitadas/inactivas); el contenido varía por corrida (SHA-256 distinto) para no chocar con duplicados. El **documento** de B no se
puede retirar por HTTP: si se acumulan demasiados, su limpieza requiere una acción administrativa autorizada del equipo (fuera de estas pruebas).
Si una corrida falla a mitad, pueden quedar cuentas habilitadas o empresas activas: sus nombres llevan `E2E <run_id>` y `resumen.json` lista sus IDs.

## Guía para mostrar Jenkins y demostrar el proyecto en UAT

1. **Jenkins (QA):** abra el pipeline de la rama `qa` → stages «Integration Tests» y «E2E Tests» tras «Deploy QA» → *Test Result* (JUnit) →
   *Artifacts* (`resumen.md` es la vista más legible: una fila por petición con HTTP, tiempo y aserciones).
2. **UAT (demostración manual, nunca automática):** Jenkins **no** ejecuta estas colecciones contra UAT (el ejecutor además se niega a correr con
   `entorno=uat` dentro de Jenkins/CI). Desde un shell del servidor con Docker, tras desplegar UAT:
   ```bash
   docker build -t igualab-pruebas-http:uat pruebas-http
   read -rp 'Correo del SuperAdmin de UAT: ' IGUALAB_E2E_SUPERADMIN_CORREO
   read -rsp 'Contraseña vigente: ' IGUALAB_E2E_SUPERADMIN_PASSWORD; echo
   export IGUALAB_E2E_SUPERADMIN_CORREO IGUALAB_E2E_SUPERADMIN_PASSWORD
   docker run --rm --network proxy_net -u "$(id -u):$(id -g)" \
     -e IGUALAB_PRUEBAS_ENTORNO=uat -e IGUALAB_CONFIRMO_ESCRITURA_UAT=SI \
     -e IGUALAB_API_URL=http://igualab-backend-uat-backend-1:8000 \
     -e IGUALAB_E2E_SUPERADMIN_CORREO -e IGUALAB_E2E_SUPERADMIN_PASSWORD \
     -v "$PWD/resultados-uat:/resultados" \
     igualab-pruebas-http:uat node /opt/pruebas-http/scripts/ejecutar.mjs --coleccion e2e --resultados /resultados
   ```
   Para mostrar un flujo concreto añada `--carpeta A` (revocación, sin OCI) o `--carpeta D` (rechazo, sin OCI). B→C usa OCI real con una muestra mínima.

## Riesgos y límites conocidos

- `npm audit` informa advisories **en las dependencias de Newman 6.2.1 (la última versión)**, p. ej. `handlebars`, `lodash`, `@faker-js/faker`. Aplican a plantillas o
  entradas **no confiables**; aquí solo se ejecutan colecciones del repositorio, en un contenedor efímero y sin exponer puertos. No se usó `npm audit fix --force`
  (degradaría a Newman 4). Se revisa al actualizar Newman.
- Importar las colecciones en la aplicación Postman es posible (`entorno/ejemplo.postman_environment.json` lista las variables), pero la subida de archivos del
  E2E espera `muestras/` relativas al directorio de trabajo; la vía soportada y reproducible es el ejecutor.
- Las pruebas fijan el contrato **actual** (p. ej. `rol` en minúsculas, `ANALYSIS_VERSION_CONFLICT` antes que `ANALYSIS_NO_ELIGIBLE_DOCUMENTS`). Si el contrato cambia, cambian
  las fuentes y se regeneran los JSON.
- No cubren módulos inexistentes (RAG, prospección) ni la ingesta de más de 50 MB (`413`) ni el reintento de publicación con un documento real.
