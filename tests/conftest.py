import os


os.environ.setdefault(
    "JWT_SECRET_KEY",
    "clave-secreta-exclusiva-para-pruebas",
)
os.environ.setdefault("MAIL_USERNAME", "pruebas@igualab.org")
os.environ.setdefault("MAIL_PASSWORD", "password-solo-para-pruebas")
os.environ.setdefault("MAIL_FROM", "pruebas@igualab.org")