
import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models import RolUsuario, SectorEmpresa



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


class CrearEmpresaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    nombre: str = Field(min_length=1, max_length=200)
    sector: SectorEmpresa


class EditarEmpresaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    nombre: str | None = Field(default=None, min_length=1, max_length=200)
    sector: SectorEmpresa | None = None

    @model_validator(mode="after")
    def validar_campos_enviados(self):
        if not self.model_fields_set:
            raise ValueError("Debe indicar el nombre o el sector que desea modificar.")
        if any(getattr(self, campo) is None for campo in self.model_fields_set):
            raise ValueError("El nombre y el sector no pueden ser nulos.")
        return self


class EmpresaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    nombre: str
    sector: SectorEmpresa
    activa: bool
    creada_en: datetime
