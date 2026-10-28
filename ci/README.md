# Control de vulnerabilidades de la imagen (Trivy)

Requisito del TA: analizar la imagen Docker **después de construirla y antes de desplegarla**.

| Severidad | Efecto |
|---|---|
| LOW | permitido |
| MEDIUM, HIGH, CRITICAL | **bloquean** el despliegue (tengan o no versión corregida) |
| UNKNOWN | pendiente de clasificación: **no se aprueba automáticamente**, también bloquea |
| Error técnico (Trivy, base de datos, lectura de la imagen, resultado inválido) | bloquea; nunca equivale a «sin vulnerabilidades» |

No hay excepciones: ni `--ignore-unfixed`, ni `.trivyignore`, ni VEX, ni exclusiones de paquetes o rutas, ni `|| true`, ni `UNSTABLE`.
Si aparece un archivo de ignorados o de configuración de Trivy en el repositorio, el análisis falla (`ejecutar-trivy.sh`, paso 1).

## Flujo (ramas `development`, `qa` y `uat`)

1. `Construir imagen` (`construir-imagen.sh`): `docker build --pull` UNA vez. La etiqueta es única por ejecución
   (`igualab-backend-<ambiente>:b<build>-<commit>`) y el ID queda en `ci-estado/imagen.env` (`IMAGEN_REF`, `IMAGEN_ID`).
2. `Trivy` (`trivy/ejecutar-trivy.sh`): `docker save` de la imagen por ID, un único `trivy image --input` con todas las severidades,
   `evaluar_trivy.py` valida y decide sobre ese mismo JSON y `trivy convert` genera el HTML desde él. Trivy y el intérprete del evaluador
   están fijados por versión y digest; el contenedor de Trivy no recibe el socket de Docker ni variables del agente.
3. `Deploy …` (`desplegar-imagen-analizada.sh`): lo único que ejecuta `docker compose down`. Comprueba que `IMAGEN_REF` sigue siendo `IMAGEN_ID`,
   hace `up -d --no-build --pull never` con `BACKEND_IMAGE` y verifica que el contenedor corre ese ID.

Si Trivy bloquea o falla, el pipeline se detiene antes de cualquier `down`: el despliegue anterior sigue funcionando.

## Reportes (artifacts de Jenkins: solo `trivy-resultados/`)

`trivy-report.html`, `trivy-report.json` (sin la configuración de la imagen), `resumen-trivy.txt` (imagen, ID, versión de Trivy, base de
vulnerabilidades, conteos, veredicto y motivo) y `consola-trivy.log`. El tar de la imagen y el JSON crudo (`trivy-trabajo/`) se borran al terminar.
Retención: `buildDiscarder(logRotator(numToKeepStr: '1', artifactNumToKeepStr: '1'))`. La caché de Trivy (solo la base de vulnerabilidades) vive
en el volumen Docker `igualab-trivy-cache-<ambiente>`, separado de los artifacts.

## Mostrar el HTML en la presentación

1. Abrir el build en Jenkins → **Artifacts** → `trivy-resultados/trivy-report.html`.
2. Jenkins sirve los artifacts con una política CSP restrictiva: la página puede verse sin colores. Para verla con el diseño completo, descargar
   el archivo y abrirlo en el navegador. No se debe relajar la CSP de Jenkins para esto.
3. El veredicto y los conteos están en `resumen-trivy.txt` del mismo directorio.

## Prueba local con Docker real

```sh
AMBIENTE=development NUMERO_BUILD=9001 sh ci/construir-imagen.sh
. ci-estado/imagen.env && export IMAGEN_REF IMAGEN_ID AMBIENTE=development
sh ci/trivy/ejecutar-trivy.sh    # 0 aprobada · 1 bloqueada · 2 error técnico
```

Pruebas sin Docker: `python -m pytest tests/ci -q` (política con resultados controlados; no sustituyen al escaneo real).

## Compatibilidad al actualizar dependencias (qué NO es una corrección de vulnerabilidades)

Para eliminar los hallazgos de Trivy se actualizaron FastAPI, Starlette, fastapi-mail, pydantic, httpx y otras dependencias, y se sustituyó python-jose
por PyJWT (python-jose 3.5.0 tiene CVE-2026-85394 sin parche). Esas actualizaciones cambian algunos comportamientos por defecto de las bibliotecas; el
contrato HTTP vigente se **conserva** explícitamente, y estas decisiones no son correcciones de vulnerabilidades:

| Decisión | Dónde | Motivo |
|---|---|---|
| `strict_content_type=False` | `app/main.py` | FastAPI >= 0.132 rechaza (422) un cuerpo JSON sin `Content-Type: application/json`; la versión anterior (0.115) lo aceptaba. Se restituye para no cambiar el contrato con clientes que no envían la cabecera. Un `Content-Type` no JSON (p. ej. `text/plain`) sigue rechazándose como antes. La API se autentica solo con `Authorization: Bearer` (sin cookies). Cubierto por `tests/test_content_type_json.py`. |
| `verify_iat=False` al decodificar el JWT | `app/core/security.py` | python-jose no rechazaba un `iat` posterior al momento actual; PyJWT sí por defecto. Se conserva el criterio anterior. Firma, algoritmo, `exp`, `sub` y `jti` siguen verificándose (`tests/test_jwt_compatibilidad.py`, con tokens emitidos por python-jose). |

Otros efectos de la actualización, sin cambio de comportamiento HTTP: el OpenAPI publica el archivo subido como `contentMediaType: application/octet-stream` (antes
`format: binary`), y desde FastAPI 0.137 `app.routes` es un árbol interno, por lo que las pruebas enumeran rutas con OpenAPI (`tests/ayudantes_rutas.py`).

## Base de la imagen y límite de cobertura de Trivy

La imagen del backend se construye sobre `python:3.13-alpine` (ver `Dockerfile`). Medición con Trivy 0.76.1 (base de vulnerabilidades del 2026-10-28) de las
bases candidatas, solo la base:

| Base | Hallazgos bloqueantes del SO | Observación |
|---|---|---|
| `python:3.13-slim` (Debian 13.7) | 103 | sin versión corregida en Debian |
| `python:3.13-slim-bookworm` (Debian 12) | 153 | peor |
| `gcr.io/distroless/python3-debian13` | 119 | mismos paquetes Debian; Python 3.13.5 |
| `cgr.dev/chainguard/python:latest` | 0 | Python 3.15.1 y solo la etiqueta `latest` gratuita: no compatible con 3.13 ni fijable |
| `python:3.13-alpine` (3.24.3) | 0 | Python 3.13; elegida |

**«0 hallazgos» NO significa «sin riesgo».** Trivy solo informa lo que figura en las fuentes de datos de cada distribución:

- **Alpine (y Wolfi):** su fuente de avisos de seguridad lista **solo vulnerabilidades que ya tienen un paquete corregido**. Un CVE sin parche **no aparece**
  aunque el paquete esté afectado; Debian, en cambio, sí publica las vulnerabilidades sin parche (por eso el mismo análisis daba cientos de hallazgos).
  El cero de Alpine refleja en parte la cobertura de la fuente, no la ausencia de vulnerabilidades.
- Trivy no detecta vulnerabilidades desconocidas ni configuraciones inseguras (aquí solo se analiza `vuln`), ni software compilado a mano sin metadatos de paquete.
- El resultado depende de la base de vulnerabilidades del momento del análisis: un build aprobado hoy puede tener hallazgos mañana. Por eso cada ejecución
  vuelve a analizar y el resumen (`resumen-trivy.txt`) registra la versión y la fecha de la base.
- No se oculta nada: pip, setuptools y wheel se **desinstalan** de la imagen final con sus archivos (no se borran solo metadatos) porque no se usan en ejecución;
  no hay `.trivyignore`, VEX, `--ignore-unfixed` ni exclusiones. UNKNOWN sigue bloqueando, pendiente de confirmación del TA.

Un análisis aprobado es una condición necesaria del despliegue, no una auditoría de seguridad: no sustituye a ZAP, a las pruebas funcionales ni a la revisión de dependencias.
