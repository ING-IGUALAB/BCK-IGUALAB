"""Envío de correo (recuperación de contraseña). El `ConnectionConfig` de fastapi-mail se construye
de forma PEREZOSA, al enviar: si falta la configuración de correo, la aplicación ARRANCA IGUAL y solo
esta operación responde 503 (`MAIL_NOT_CONFIGURED`). Antes se construía en el import y un `MAIL_*`
vacío rompía el arranque completo de la API (bug P0)."""
from __future__ import annotations

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableError


def _configurado() -> bool:
    """True si hay correo remitente configurado (MAIL_USERNAME y MAIL_FROM)."""
    return bool(settings.MAIL_USERNAME and settings.MAIL_FROM)


def _construir_config() -> ConnectionConfig:
    return ConnectionConfig(
        MAIL_USERNAME=settings.MAIL_USERNAME,
        MAIL_PASSWORD=settings.MAIL_PASSWORD,
        MAIL_FROM=settings.MAIL_FROM,
        MAIL_FROM_NAME=settings.MAIL_FROM_NAME,
        MAIL_SERVER=settings.MAIL_SERVER,
        MAIL_PORT=settings.MAIL_PORT,
        MAIL_STARTTLS=settings.MAIL_STARTTLS,
        MAIL_SSL_TLS=settings.MAIL_SSL_TLS,
        USE_CREDENTIALS=True,
    )


async def enviar_correo_recuperacion(destinatario: str, token_plano: str) -> None:
    if not _configurado():
        raise ServiceUnavailableError(
            "MAIL_NOT_CONFIGURED",
            "El envío de correos no está disponible: falta configurar MAIL_USERNAME / MAIL_FROM.",
        )
    try:
        mailer = FastMail(_construir_config())
    except Exception as exc:  # valores presentes pero inválidos (p. ej. MAIL_FROM sin '@')
        raise ServiceUnavailableError(
            "MAIL_NOT_CONFIGURED",
            "El envío de correos no está disponible: la configuración de correo es inválida.",
        ) from exc

    enlace = f"{settings.FRONTEND_RESET_URL}?token={token_plano}"
    mensaje = MessageSchema(
        subject="Igualab · Recuperación de contraseña",
        recipients=[destinatario],
        body=f"""
            <p>Recibimos una solicitud para restablecer tu contraseña.</p>
            <p><a href="{enlace}">Haz clic aquí para crear una nueva contraseña</a></p>
            <p>Este enlace vence en {settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES} minutos. Si no solicitaste esto, ignora este correo.</p>
        """,
        subtype=MessageType.html,
    )
    await mailer.send_message(mensaje)
