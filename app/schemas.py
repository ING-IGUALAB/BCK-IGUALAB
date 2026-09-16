
import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr, field_validator

from app.models import RolUsuario



class LoginRequest(BaseModel):
    correo: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    rol: RolUsuario
    nombre: str


class SolicitarRecuperacionRequest(BaseModel):
    correo: EmailStr


class RestablecerPasswordRequest(BaseModel):
    token: str
    password_nueva: str
    password_nueva_confirmacion: str

    @field_validator("password_nueva_confirmacion")
    @classmethod
    def confirmacion_coincide(cls, v, info):
        if "password_nueva" in info.data and v != info.data["password_nueva"]:
            raise ValueError("Las contraseñas no coinciden.")
        return v


class CambiarPasswordRequest(BaseModel):
    password_actual: str
    password_nueva: str



class CrearUsuarioRequest(BaseModel):
    nombre: str
    correo: EmailStr
    password: str


class UsuarioResponse(BaseModel):
    id: uuid.UUID
    nombre: str
    correo: EmailStr
    rol: RolUsuario
    habilitado: bool
    creado_en: datetime

    class Config:
        from_attributes = True


class TransferirSuperAdminRequest(BaseModel):
    cuenta_destino_id: uuid.UUID
