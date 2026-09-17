from __future__ import annotations
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _valid_request_id(value: str | None) -> str:
    if value:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError):
            pass
    return str(uuid.uuid4())


class RequestIDMiddleware:
    """Asocia un UUID a la solicitud y lo devuelve en la respuesta."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_request_id = headers.get(b"x-request-id")
        supplied_request_id = (
            raw_request_id.decode("ascii", errors="ignore") if raw_request_id else None
        )
        request_id = _valid_request_id(supplied_request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_with_request_id)
