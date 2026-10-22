# OWASP ZAP API Scan del backend (stage `OWASP ZAP`)

Análisis de seguridad **dinámico** del backend **ya desplegado en QA**: ZAP importa el OpenAPI real (filtrado), se autentica con sesiones
reales y ejecuta el **análisis activo**. Va en `Jenkinsfile` **después de `E2E Tests`**, se llama exactamente `OWASP ZAP` y **solo corre en la
rama `qa`** (se desarrolla en `development` y se promueve por PR). Nunca se ejecuta contra UAT ni contra la rama `development`.

> **Qué NO demuestra.** Un build verde significa «sin alertas FAIL/WARN en lo escaneado», no «backend seguro». La cobertura **no es completa**:
> las operaciones que consumen OCI, DeepSeek o envían correo están excluidas **a la espera de aceptación del TA** (ver «Alcance»).
> El resultado de Jenkins es la única evidencia de una ejecución; ningún resultado de este documento lo anticipa.

## Piezas

```
pruebas-zap/
├─ alcance/operaciones.json         FUENTE ÚNICA del alcance: cada operación (método + ruta) incluida/excluida, con rol, categoría y motivo
├─ reglas/zap-api-scan.reglas.conf  reglas FAIL / WARN / IGNORE de zap-api-scan.py (cada IGNORE justificado en el propio archivo)
├─ hooks/zap_hooks.py               hooks de ZAP: inyecta las credenciales por rol, vigila el alcance y escribe la evidencia de cobertura
├─ scripts/ejecutar-zap.sh          orquestador POSIX sh que ejecuta Jenkins (solo necesita Docker en el nodo)
├─ scripts/preparar.mjs · finalizar.mjs · lib/   preparación y cierre (Node 22, solo biblioteca estándar, en contenedor)
├─ Dockerfile                       imagen de preparación/cierre (mismo Node con digest que pruebas-http)
├─ scripts/pruebas/ · pruebas/      verificación local aislada (docker y backend FALSOS; ver «Verificación»)
tests/test_alcance_zap.py           guard pytest: una operación nueva del OpenAPI sin clasificar falla ya en cualquier rama
```

## ZAP: versión y comando

| | |
|---|---|
| Imagen | `zaproxy/zap-stable:2.17.0@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6` |
| Qué es el digest | índice multi-arquitectura (amd64/arm64) de la etiqueta `2.17.0`; **comprobado contra el registro de Docker Hub** el 2026-10-22 (la etiqueta `latest` apuntaba al mismo). Docker valida el contenido descargado contra el digest: si no coincide, el `pull` falla y el build falla. |
| Por qué no `stable` | es una etiqueta móvil: cambia sin aviso. La imagen se reconstruye semanalmente con la misma versión 2.17.0, por eso se fija el digest. |
| Para actualizar | elegir una versión, leer su digest en el registro, cambiar `ZAP_IMAGEN` en `ejecutar-zap.sh` y revisar los códigos de salida y las opciones de `zap-api-scan.py` de esa versión. |
| Complementos | el script oficial ejecuta `-addonupdate`: ZAP actualiza sus complementos **al arrancar** (necesita salida a Internet desde proxy_net). El digest fija el motor, no las reglas. |

Comando efectivo (lo imprime el stage; no contiene secretos):

```
docker run -d --name igualab-zap-<BUILD_TAG> --network proxy_net --label igualab.zap.ejecucion=<BUILD_TAG> \
  -v igualab-zap-trabajo-<BUILD_TAG>:/zap/wrk:rw -v igualab-zap-privado-<BUILD_TAG>:/zap/privado:ro \
  zaproxy/zap-stable:2.17.0@sha256:5ccfff9a… zap-api-scan.py -t openapi-filtrado.json -f openapi \
  -c zap-api-scan.reglas.conf -r backend-zap.html -J backend-zap.json --hook=/zap/wrk/zap_hooks.py -T 10 \
  -z "-config scanner.threadPerHost=3 -config scanner.delayInMs=0 -config scanner.maxScanDurationInMins=60 -config scanner.maxRuleDurationInMins=5"
```

* **Análisis activo habilitado.** No se usa `-S` (baseline). Al pasar `-c`, el script oficial activa **todas** las reglas activas de la política por
  defecto salvo las marcadas `IGNORE`. No se usa `-I`: así las advertencias dan el código 2.
* Una ejecución solo se acepta como análisis activo si el hook comprueba que hubo un escaneo activo con peticiones de ataque (`cobertura-zap.json`
  → `analisis_activo`). Si ZAP lo interrumpe por tiempo (`maxScanDurationInMins`), el resultado es **INESTABLE**, nunca éxito.

### Reproducirlo a mano (nodo con Docker, **solo QA**)

```sh
export IGUALAB_API_URL=http://igualab-backend-qa-backend-1:8000 IGUALAB_PRUEBAS_ENTORNO=qa
export IGUALAB_E2E_SUPERADMIN_CORREO=...                       # la contraseña con: read -rs IGUALAB_E2E_SUPERADMIN_PASSWORD; export IGUALAB_E2E_SUPERADMIN_PASSWORD
sh pruebas-zap/scripts/ejecutar-zap.sh                          # deja zap-resultados/ en la raíz del repositorio (los datos viven en volúmenes de Docker)
```

## Qué hace el stage, en orden

1. **Verifica el destino**: entorno `qa`; en Jenkins, rama `qa`; el host debe identificar a QA (o confirmarse con `ZAP_CONFIRMO_DESTINO_QA`) y no parecer UAT/dev/prod;
   si el host es un contenedor del nodo, su proyecto Compose debe ser `igualab-backend-qa` y estar en `proxy_net`.
2. **Disponibilidad** con plazo acotado (`/health`, 240 s por defecto) y **`/openapi.json`** accesible, JSON válido, OpenAPI 3.x, **sin `servers` ni `$ref` hacia otros ambientes**.
3. **Alcance**: cada operación del OpenAPI debe estar clasificada en `alcance/operaciones.json`; una nueva sin clasificar **detiene** la preparación.
   Se deriva `openapi-filtrado.json` solo con las operaciones incluidas (un único `servers`: el destino) y ejemplos que dirigen las peticiones al recurso sintético.
   El OpenAPI publicado por la aplicación no se modifica.
4. **Datos sintéticos**: un solo login del SuperAdmin de QA; se crean **una** cuenta Administrador y **una** empresa sintéticas (`ZAP <id>`); se demuestra el acceso real
   (ver «Cobertura autenticada»).
5. **ZAP** en contenedor propio (`--network proxy_net`), con el límite global y el hook.
6. **Cierre (siempre)**: desactiva la empresa, deshabilita la cuenta (lo que revoca sus sesiones), cierra la sesión del escáner del SuperAdmin y comprueba las revocaciones;
   valida y **redacta** los reportes; cruza la evidencia del hook y decide el código de salida.

## Alcance (método + ruta)

Fuente: `alcance/operaciones.json` (33 operaciones del OpenAPI real: **19 incluidas, 14 excluidas**). Tablas generadas de ese archivo el 2026-10-22.

**Importante: ZAP, al importar un OpenAPI, ENVÍA una petición por operación.** Por eso las exclusiones se aplican a la especificación que se importa
(descubrimiento/importación) y no solo al ataque activo. Además el hook excluye del ataque las rutas con todos sus métodos vetados y **comprueba antes del ataque
y al terminar** que ninguna petición fue a una operación excluida o fuera del destino (violación → código 11).

### Incluidas

| Método | Ruta | Sesión usada |
|---|---|---|
| GET | `/health` | pública |
| POST | `/auth/login` | pública (identidad **inexistente**, ver abajo) |
| GET | `/usuarios` | SuperAdmin |
| GET | `/empresas` | SuperAdmin |
| GET | `/empresas/{empresa_id}` | SuperAdmin (empresa sintética) |
| PATCH | `/empresas/{empresa_id}` | SuperAdmin (empresa sintética) |
| PATCH | `/empresas/{empresa_id}/activar` | SuperAdmin (empresa sintética) |
| GET | `/documentos/operaciones/{operacion_id}` | SuperAdmin |
| GET | `/documentos` | SuperAdmin |
| GET | `/documentos/{documento_id}` | SuperAdmin |
| GET | `/auditoria` | SuperAdmin |
| GET | `/analisis-gri/empresas/{empresa_id}/anios/{anio}` | Administrador sintético |
| PUT | `/analisis-gri/empresas/{empresa_id}/anios/{anio}/codigos/{identidad}/estado` | Administrador sintético |
| GET | `/analisis-gri/empresas/{empresa_id}/anios/{anio}/historial` | Administrador sintético |
| POST | `/analisis-gri/empresas/{empresa_id}/anios/{anio}/validacion` | Administrador sintético |
| POST | `/reportes/vista-previa` | Administrador sintético |
| GET | `/reportes` | Administrador sintético |
| POST | `/reportes` | Administrador sintético (sin análisis validado: 409/404, no crea reportes) |
| GET | `/reportes/{reporte_id}/archivo` | Administrador sintético |

`POST /auth/login` se escanea con una identidad **sin cuenta** (`zap-escaner-sin-cuenta@igualab-zap.example.com`), de modo que ninguna cuenta real puede bloquearse (5 intentos).
Efecto conocido: eventos de auditoría de inicio de sesión fallido anónimo en QA.

### Excluidas

| Método | Ruta | Categoría | Motivo |
|---|---|---|---|
| POST | `/asistente/consultas` | servicio_externo | Consume OCI (embeddings) y DeepSeek (chat). **Pendiente de aceptación del TA.** |
| POST | `/documentos/operaciones/{operacion_id}/ingesta` | servicio_externo | Genera embeddings (OCI), escribe en MinIO y pgvector. **Pendiente de aceptación del TA.** |
| POST | `/documentos/operaciones/{operacion_id}/reintentar-publicacion` | servicio_externo | Repite la publicación vectorial (pgvector/MinIO). **Pendiente de aceptación del TA.** |
| POST | `/auth/recuperar-contrasena` | servicio_externo | Envía correo (SMTP) y genera tokens de restablecimiento. **Pendiente de aceptación del TA.** |
| POST | `/auth/logout` | sesion | Revocaría la sesión del escáner. |
| PATCH | `/auth/mi-contrasena` | credenciales | Cambia la contraseña de la cuenta autenticada. |
| POST | `/auth/restablecer-contrasena` | credenciales | Restablece credenciales mediante un token. |
| POST | `/usuarios` | cuentas | Crearía cuentas Administrador habilitadas con valores del escáner. |
| PATCH | `/usuarios/{usuario_id}/habilitar` | cuentas | Muta cuentas (posiblemente ajenas o la del SuperAdmin). |
| PATCH | `/usuarios/{usuario_id}/deshabilitar` | cuentas | Revocaría sesiones, incluidas las del escáner. |
| POST | `/usuarios/transferir-superadmin` | cuentas | Transferiría el rol SuperAdmin (prohibido). |
| POST | `/empresas` | datos_persistentes | Crearía empresas activas con nombres del escáner; no hay borrado por HTTP. |
| PATCH | `/empresas/{empresa_id}/desactivar` | estado_del_escaneo | Dejaría inactiva la única empresa sintética y el resto recibiría `COMPANY_INACTIVE` (el cierre ya la desactiva). |
| POST | `/documentos/operaciones` | datos_persistentes | Solo alimenta la ingesta (excluida) y deja operaciones huérfanas. |

**Pendientes de decisión.** (1) El TA debe aceptar —o autorizar un tratamiento distinto para— las operaciones de OCI/DeepSeek/correo; hasta entonces **no se afirma cobertura completa**.
(2) Las exclusiones por credenciales/cuentas/datos persistentes son decisiones de esta implementación: si se quieren cubrir (p. ej. `POST /empresas` con limpieza por ventana de tiempo),
cambie `alcance/operaciones.json` **y** diseñe su limpieza; el test `tests/test_alcance_zap.py` protege las exclusiones sensibles.

## Autenticación y cobertura autenticada

* **Credencial de Jenkins (única necesaria):** `IGUALAB_QA_SUPERADMIN` (*Username with password*: correo y contraseña **vigente** del SuperAdmin de QA), la misma de `Integration Tests` y `E2E Tests`.
  Opcional: variable de entorno `IGUALAB_QA_API_URL` si el backend no se alcanza por `http://igualab-backend-qa-backend-1:8000`. No se crea otro SuperAdmin ni se transfiere el rol.
* **No se reutilizan tokens de E2E** (su cierre los revoca): el ejecutor abre su propia sesión del SuperAdmin (un solo intento: ante 401/423 se detiene para no bloquear la cuenta) y crea
  un Administrador sintético con contraseña aleatoria que solo vive en memoria del contenedor.
* **Dos roles, una sola corrida:** el hook añade dos reglas de *Replacer* que ponen `Authorization: Bearer …` filtrando por URL: `usuarios|empresas|documentos|auditoria` → SuperAdmin;
  `analisis-gri|reportes` → Administrador. El token solo se envía al destino de QA. La cuenta del SuperAdmin **no es destino** de ninguna operación mutadora incluida.
* **Cómo se demuestra que no es «todo 401/403»:** (a) la preparación exige 200 en `GET /usuarios`, `/auditoria`, `/empresas/{id}` (SuperAdmin) y `/reportes`, `/empresas?solo_activas=true` (Administrador; sin el filtro el contrato real responde 403), además de
  403 del Administrador en `/usuarios` y 401 sin sesión; (b) al terminar, el hook revisa el historial de ZAP y exige, **por rol**, respuestas autenticadas que alcanzaron la lógica (no 401/403) y al menos
  un 2xx. Si no, el build falla con el código 9. `cobertura-zap.json` lista las operaciones incluidas sin ninguna respuesta autenticada de la lógica (p. ej. rutas que solo devuelven 404/409
  porque la empresa sintética no tiene documentos): su cobertura autenticada **no** está demostrada y debe leerse como tal.

## Reglas FAIL / WARN / IGNORE

`reglas/zap-api-scan.reglas.conf` (formato oficial `id<TAB>NIVEL<TAB>(mensaje)`). FAIL: inyección SQL, XSS, path traversal, inclusión remota de ficheros, ejecución de código/comandos del servidor,
XXE, XPath, EL, SSTI y SSI. WARN (por defecto y explícito): errores 5xx, cabeceras y divulgación. **IGNORE solo 4 reglas**, todas como **cobertura excluida** (no se ejecutan; no son vulnerabilidades descartadas):
  * `40043` Log4Shell, `40046` SSRF, `40047` Text4Shell: requieren un servicio OAST externo (sacarían tráfico de QA a un tercero), así que **no se evaluaron**; **SSRF queda sin cubrir**.
  * `40025` Proxy Disclosure: *bypass* indicado por el asesor del curso para todo el grupo, porque el proxy Caddy la dispara como Medium. Es la **única** excepción de este tipo; no se evalúa la exposición del proxy y debe
    revisarse por otro medio (cabeceras `Via`/`Server` del endpoint público). 

  * **Comprobado en local** con la imagen fijada (53 reglas activas): `40043` existe y su `SKIP` se observó; `40025`, `40046` y `40047` **no existen** en esa imagen. Para esas tres el IGNORE es preventivo (un complemento beta
    podría añadirlas al actualizarse en ejecución) y, en esta imagen, ni SSRF ni Proxy Disclosure se evalúan. El efecto real del bypass de `40025` solo podrá observarse en Jenkins.

No se ignora ninguna alerta para obtener verde: un FAIL se corrige o se documenta aquí.

## Códigos de salida → Jenkins

| Código | Significado | Resultado del build |
|---|---|---|
| 0 | Éxito (sin FAIL ni WARN; análisis activo completo; evidencia presente) | SUCCESS |
| 2 | Advertencias WARN sin FAIL, o análisis activo parcial | **UNSTABLE** |
| 1 | Reglas FAIL | FAILURE |
| 3 | ZAP terminó con otro código (error técnico) | FAILURE |
| 4 / 5 | Configuración o destino no permitido / backend no disponible | FAILURE |
| 6 | Tiempo agotado o cancelación | FAILURE |
| 7 | Faltan o son inválidos `backend-zap.html`/`backend-zap.json` | FAILURE |
| 8 | Quedó un secreto en un artefacto (no se archivó) | FAILURE |
| 9 | Sin evidencia: hook sin resumen, sin análisis activo o sin cobertura autenticada | FAILURE |
| 10 | No se pudo cerrar la cuenta o la empresa sintéticas | FAILURE |
| 11 | Petición a una operación excluida o fuera del destino | FAILURE |
| 12 | Un directorio de datos no es escribible o falló una operación de archivo (permisos/propietario) | FAILURE |

Nada se oculta con `exit 0` ni `|| true`; un reporte ausente o vacío nunca se presenta como análisis exitoso.

## Límites de tiempo

| Límite | Valor | Dónde |
|---|---|---|
| Stage completo | **100 min** | `timeout` del `Jenkinsfile` |
| Contenedor de ZAP | **80 min** (`ZAP_LIMITE_MINUTOS`) | `ejecutar-zap.sh`; al superarse se detiene y elimina **solo** `igualab-zap-<BUILD_TAG>` (se verifica su etiqueta antes) |
| Análisis activo | 60 min en total, 5 min por regla | `-config scanner.maxScanDurationInMins / maxRuleDurationInMins` |
| Backend disponible | 240 s (`ZAP_ESPERA_SEGUNDOS`) | preparación |

Son valores **iniciales razonables**, no medidos: tras la primera ejecución real en QA conviene calibrarlos (hilos `ZAP_HILOS`=3 y `ZAP_RETARDO_MS`=0 también). Si Jenkins cancela o agota el stage,
el `post` ejecuta `ejecutar-zap.sh limpiar`: elimina el contenedor de ZAP de la ejecución y completa el cierre de la cuenta y la empresa sintéticas si faltó (idempotente).

## Artefactos y secretos

Se archiva **solo** `zap-resultados/` (incluso si el stage falla):

| Archivo | Contenido |
|---|---|
| `backend-zap.html`, `backend-zap.json` | Reportes de ZAP, con la evidencia útil (URL, método, parámetro, ataque, evidencia) y **sin secretos** |
| `resumen-zap.md` / `resumen-zap.json` | Veredicto, alcance, cobertura autenticada, limpieza, reportes, limitaciones pendientes |
| `alcance-zap.json`, `openapi-filtrado.json` | Qué se incluyó/excluyó y por qué; la especificación que se importó |
| `cobertura-zap.json` | Evidencia del hook: análisis activo real, cobertura por rol y por operación, violaciones de alcance |
| `consola-zap.log` | Consola filtrada |

Estado, credenciales y reportes crudos **nunca** se archivan: viven en tres **volúmenes nombrados de Docker** (`igualab-zap-trabajo|privado|resultados-<BUILD_TAG>`) que se eliminan al terminar.
Se redactan JWT, `Authorization`/`Bearer` y las contraseñas/tokens de la ejecución (también escapados para JSON/HTML/URL), en consola y en los artefactos; después se **verifica** que no quede ninguno (si queda,
ese archivo se elimina y el código es 8). Los tokens se entregan a ZAP en un archivo 0600 de un volumen 0700 montado de solo lectura, no por línea de comandos ni en el espacio de trabajo.

## Permisos y propietarios (por qué volúmenes)

**Fallo del build 36 (QA):** la preparación terminó con `Error (EACCES)` y el cierre también falló, sin dejar resumen. Reproducido con Docker real: la **primera escritura** de la preparación tras `Alcance: …` es
`writeFileSync('/privado/estado.json')`, y devuelve `EACCES` cuando el uid del contenedor no es dueño de `/privado` (p. ej. directorio creado como root 755 por otro contenedor o por el daemon). La causa de que en el nodo
de QA esa carpeta no perteneciera al uid del contenedor **no se pudo determinar** con los artifacts (ver «Causas posibles»); la corrección elimina la dependencia.

**Corrección:**
* Los datos ya no están en carpetas del espacio de trabajo de Jenkins. Son volúmenes nombrados que **heredan de la imagen** `pruebas-zap` el propietario `1000:1000` (usuario `node` de la imagen y `zap` del contenedor de ZAP) y el modo
  (`/privado` 0700, `/trabajo` y `/resultados` 0750). Los tres contenedores (preparación, ZAP, cierre) corren como `1000:1000`: **ninguno es root y no hay `chmod 777`**.
* Al anfitrión solo vuelve, con `docker cp` (propiedad del usuario de Jenkins), `zap-resultados/` ya redactada, así que `deleteDir()` no choca con archivos de otro usuario.
* La preparación comprueba **antes de tocar el backend** que puede escribir en `/trabajo` y `/privado`; si no, sale con el código **12** y un mensaje con **fase, operación, ruta del contenedor, código de error, uid/gid y propietario/modo de la carpeta**
  (nunca contenido ni secretos). El cierre tolera una preparación incompleta (estado ilegible o truncado: busca los recursos por su nombre determinista `zap-<id>@igualab-zap.example.com` / `ZAP <id> Empresa` y, si no puede comprobarlo, **no** da la limpieza por buena),
  informa qué pudo limpiar y, si no puede escribir `/resultados`, muestra el resumen por consola y sale con 12. Si el cierre no deja su resumen, `ejecutar-zap.sh` escribe uno mínimo: nunca queda un artifact sin explicación.
* El log de consola empieza con el uid:gid del orquestador, si corre dentro de un contenedor y las opciones de seguridad de Docker (`rootless`, `userns`, `selinux`…), para distinguir la causa si algo vuelve a fallar.

**Causas posibles del propietario incorrecto en QA (no determinadas):** Docker rootless o con user namespaces (el uid del contenedor no coincide con el dueño de la carpeta), SELinux enforcing sin etiqueta de montaje, o Jenkins
dentro de un contenedor cuyo `$WORKSPACE` no existe en el anfitrión del daemon (Docker crea entonces la carpeta de montaje como root). Los volúmenes nombrados no dependen de ninguna de las tres.

**Regresión:** `sh pruebas-zap/pruebas/permisos-real.sh` (Linux con Docker; sin red ni root en el anfitrión) reproduce el caso con uid no root y carpetas «de otro contenedor» y comprueba el diagnóstico, el cierre tolerante, el resumen garantizado y los
propietarios de los volúmenes. `node --test pruebas-zap/scripts/pruebas/permisos.test.mjs` cubre la parte portable (y la de Docker cuando está disponible).

## Retención y limpieza

`buildDiscarder(logRotator(numToKeepStr: '1', artifactNumToKeepStr: '1'))` y `disableConcurrentBuilds()` se conservan. `deleteDir()` está en el `post { always }` general, **después** de
`rm -f .env .env.nuevo` y de que cada stage archive. No se borran volúmenes, bases, imágenes ni contenedores del producto; solo se elimina el contenedor de ZAP de la ejecución.
Residuo conocido en QA: la cuenta y la empresa sintéticas quedan deshabilitada/inactiva (no hay borrado por HTTP) y eventos de auditoría.

## HTTP interno vs. endpoint público

El destino es la URL **interna HTTP** del contenedor en `proxy_net`. Permite analizar la API, pero **no demuestra** la configuración TLS, HSTS, ni las cabeceras que añade el proxy público
(CSP, `X-Content-Type-Options`, `X-Frame-Options`…). Las alertas por cabeceras ausentes se emiten como WARN y deben contrastarse con el endpoint público HTTPS antes de darlas por válidas o por falsas.

## Cómo revisar los hallazgos en Jenkins

1. Abrir el build de `qa` → stage **OWASP ZAP** (tras *E2E Tests*) → *Artifacts* → `zap-resultados/`.
2. Leer **`resumen-zap.md`** primero: veredicto, código, alcance, cobertura autenticada, limpieza y limitaciones.
3. Si es **FAILURE por FAIL (1)** o **UNSTABLE (2)**: abrir `backend-zap.html`; ordenar por riesgo; cada alerta trae URL, método, parámetro, ataque y evidencia. El nivel de cada regla está en `resumen-zap.json` → `alertas`.
4. Si es **FAILURE técnico (≥3)**: leer el «Motivo» de `resumen-zap.md` y `consola-zap.log`; el análisis **no** se considera realizado. Comprobar luego `limpieza` (cuenta/empresa sintéticas).
5. Para un FAIL real: corregir el código; para un falso positivo: documentar la excepción en `reglas/zap-api-scan.reglas.conf` con su motivo (no bajar el nivel para obtener verde).
6. No olvidar las **limitaciones**: la parte excluida (OCI/DeepSeek/correo) sigue pendiente del TA.

## Verificación (qué se hizo y qué no)

Local y aislado, **sin tocar QA/UAT ni proveedores reales** (`docker` y backend **falsos**: solo prueban la orquestación, no son un escaneo del sistema desplegado):

```sh
node --test pruebas-zap/scripts/pruebas/unidad.test.mjs pruebas-zap/scripts/pruebas/ejecutor.test.mjs
python -I pruebas-zap/pruebas/test_zap_hooks.py
python -m pytest tests/test_alcance_zap.py -q
```

**No verificado hasta que corra Jenkins en QA:** el arranque real de ZAP 2.17.0 con el hook, que la importación de este OpenAPI 3.1 produzca las URLs esperadas, los `ejemplos` del OpenAPI como valores de las
peticiones, la duración real del análisis activo, la ausencia real de hallazgos, la salida a Internet de `proxy_net` (descarga del digest y actualización de complementos de ZAP) y la sintaxis declarativa del `Jenkinsfile` (no hay linter local).
