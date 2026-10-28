"""Cabeceras de seguridad que ZAP señalaba como ausentes en las respuestas de la propia aplicación.

* ``X-Content-Type-Options: nosniff``: el navegador respeta el ``Content-Type`` declarado (JSON, PDF) y no «adivina» otro. No afecta al
  uso normal de la API.
* ``Cross-Origin-Resource-Policy: same-origin``: solo páginas del mismo origen pueden incrustar estas respuestas como subrecurso sin
  CORS (``<img>``, ``<script>``…); ``same-site`` se consideraba menos estricto (ZAP 90004, build QA 41). Justificación (evidencia del
  backend): todo recurso protegido, PDF incluidos, exige la cabecera ``Authorization: Bearer`` (``HTTPBearer``; no hay cookies ni
  token en la URL), así que un navegador solo puede pedirlo con ``fetch``/XHR en modo CORS, que CORP no restringe: el frontend
  (otro origen, autorizado por ``CORS_ORIGINS``) sigue funcionando. Lo que se bloquea es incrustar la API con ``<img src>``,
  ``<embed>``, ``<script src>``, etc. (no-cors), que ya no funcionaba por faltar el Bearer. SUPUESTO NO VERIFICADO: el código del
  frontend (cómo muestra los PDF) no está disponible; confirmar en QA el flujo de descarga y de vista previa antes de promover.

No toca CORS, CSP ni Cache-Control. Se registra por FUERA del manejador de excepciones (para alcanzar también el 500 uniforme) y por
DENTRO de CORSMiddleware, que debe seguir siendo el más externo; por eso los preflight que contesta el propio CORS no las llevan (no son
recursos de la API). Una cabecera ya fijada por la ruta no se sobrescribe.
"""
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CABECERAS_DE_SEGURIDAD: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"cross-origin-resource-policy", b"same-origin"),
)


class CabecerasDeSeguridadMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_con_cabeceras(mensaje: Message) -> None:
            if mensaje["type"] == "http.response.start":
                cabeceras = list(mensaje.get("headers", []))
                presentes = {nombre.lower() for nombre, _ in cabeceras}
                cabeceras.extend((n, v) for n, v in CABECERAS_DE_SEGURIDAD if n not in presentes)
                mensaje["headers"] = cabeceras
            await send(mensaje)

        await self.app(scope, receive, send_con_cabeceras)
