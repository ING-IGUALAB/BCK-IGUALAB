
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from app.config import settings

_conf = ConnectionConfig(
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

_mailer = FastMail(_conf)


async def enviar_correo_recuperacion(destinatario: str, token_plano: str) -> None:
    
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
    await _mailer.send_message(mensaje)