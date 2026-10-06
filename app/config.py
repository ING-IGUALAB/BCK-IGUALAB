
import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


def _csv_env(name: str, default: str = "") -> list[str]:
    return [value.strip().rstrip("/") for value in os.getenv(name, default).split(",") if value.strip()]

class Settings:

    APP_ENV: str = os.getenv("APP_ENV", "development")

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://usuario:password@localhost:5432/igualab"
    )
    VECTOR_DATABASE_URL: str = os.getenv("VECTOR_DATABASE_URL", DATABASE_URL)

    BACKEND_URL: str = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")
    CORS_ORIGINS: list[str] = _csv_env("CORS_ORIGINS", FRONTEND_URL)

    # JWT (RNF-003, RNF-025)
    JWT_SECRET_KEY: str = os.environ["JWT_SECRET_KEY"]
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")

    # expiración de sesión por inactividad, 2 horas
    SESSION_INACTIVITY_TIMEOUT: timedelta = timedelta(
        hours=int(os.getenv("SESSION_INACTIVITY_HOURS", "2"))
    )

    # Recuperación de contraseña
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = int(
        os.getenv("PASSWORD_RESET_TOKEN_EXPIRE_MINUTES", "30")
    )

    # Bloqueo por intentos fallidos
    MAX_LOGIN_ATTEMPTS: int = int(os.getenv("MAX_LOGIN_ATTEMPTS", "5"))
    LOGIN_LOCKOUT_MINUTES: int = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))

    # Correo Gmail SMTP 
    MAIL_USERNAME: str = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD: str = os.getenv("MAIL_PASSWORD") # contraseña de aplicación
    MAIL_FROM: str = os.getenv("MAIL_FROM", os.getenv("MAIL_USERNAME"))
    MAIL_FROM_NAME: str = "Igualab"
    MAIL_SERVER: str = "smtp.gmail.com"
    MAIL_PORT: int = 587
    MAIL_STARTTLS: bool = True
    MAIL_SSL_TLS: bool = False


    # Almacenamiento de originales de ingesta (MinIO, API S3). Se leen como texto y
    # se validan solo al construir el adaptador (`config_desde_settings`), para que
    # un valor mal escrito no impida arrancar el resto de la aplicación. Sin
    # valores por defecto para endpoint, bucket, credenciales ni prefijo.
    MINIO_ENDPOINT_URL: str | None = os.getenv("MINIO_ENDPOINT_URL")
    MINIO_BUCKET: str | None = os.getenv("MINIO_BUCKET")
    MINIO_REGION: str | None = os.getenv("MINIO_REGION")  # opcional: sin confirmar
    MINIO_ACCESS_KEY: str | None = os.getenv("MINIO_ACCESS_KEY")
    MINIO_SECRET_KEY: str | None = os.getenv("MINIO_SECRET_KEY")
    MINIO_PREFIX: str | None = os.getenv("MINIO_PREFIX")  # development | qa | uat
    # Plazos provisionales en segundos (D23); calibrar con la prueba de 50 MB.
    MINIO_CONNECT_TIMEOUT_SECONDS: str = os.getenv("MINIO_CONNECT_TIMEOUT_SECONDS", "10")
    MINIO_READ_TIMEOUT_SECONDS: str = os.getenv("MINIO_READ_TIMEOUT_SECONDS", "60")
    MINIO_OPERATION_TIMEOUT_SECONDS: str = os.getenv("MINIO_OPERATION_TIMEOUT_SECONDS", "300")

    FRONTEND_RESET_URL: str = os.getenv(
        "FRONTEND_RESET_URL", f"{FRONTEND_URL}/restablecer"
    )

settings = Settings()
