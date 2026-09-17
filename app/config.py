
import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

class Settings:
    
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://usuario:password@localhost:5432/igualab"
    )

    # JWT (RNF-003, RNF-025)
    JWT_SECRET_KEY: str = os.environ["JWT_SECRET_KEY"]
    JWT_ALGORITHM: str = "HS256"

    # expiración de sesión por inactividad, 2 horas
    SESSION_INACTIVITY_TIMEOUT: timedelta = timedelta(hours=2)

    # Recuperación de contraseña
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30

    # Bloqueo por intentos fallidos
    MAX_LOGIN_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15

    # Correo Gmail SMTP 
    MAIL_USERNAME: str = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD: str = os.getenv("MAIL_PASSWORD") # contraseña de aplicación
    MAIL_FROM: str = os.getenv("MAIL_FROM", os.getenv("MAIL_USERNAME"))
    MAIL_FROM_NAME: str = "Igualab"
    MAIL_SERVER: str = "smtp.gmail.com"
    MAIL_PORT: int = 587
    MAIL_STARTTLS: bool = True
    MAIL_SSL_TLS: bool = False


    FRONTEND_RESET_URL: str = os.getenv("FRONTEND_RESET_URL", "http://localhost:5173/restablecer")

settings = Settings()
