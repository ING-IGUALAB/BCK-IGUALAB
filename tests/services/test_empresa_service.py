from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateIndex, CreateTable

from app.exceptions import ConflictError
from app.models import Empresa, SectorEmpresa
from app.schemas import CrearEmpresaRequest
from app.services import empresa_service


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicado", [True, False])
async def test_error_en_commit_revierte_transaccion(duplicado):
    db = MagicMock()
    resultado = MagicMock()
    resultado.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=resultado)
    nombre = "uq_empresas_nombre_normalizado" if duplicado else "otra_restriccion"
    error = IntegrityError("INSERT", {}, Exception(nombre))
    db.commit = AsyncMock(side_effect=error)
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    esperado = ConflictError if duplicado else IntegrityError
    with pytest.raises(esperado) as exc:
        await empresa_service.crear_empresa(db, CrearEmpresaRequest(nombre="Empresa", sector="MINERIA"))
    if duplicado:
        assert exc.value.code == "COMPANY_ALREADY_EXISTS"
    db.rollback.assert_awaited_once()
    db.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicado_con_nombre_de_restriccion_asyncpg():
    causa = Exception("unique violation")
    causa.constraint_name = "uq_empresas_nombre_normalizado"
    original = Exception("adapted")
    original.__cause__ = causa
    db = MagicMock()
    db.commit = AsyncMock(side_effect=IntegrityError("INSERT", {}, original))
    db.rollback = AsyncMock()
    with pytest.raises(ConflictError):
        await empresa_service._guardar(db, Empresa())
    db.rollback.assert_awaited_once()


def test_indice_impide_duplicado_sin_validacion_del_servicio():
    engine = create_engine("sqlite://")
    Empresa.__table__.create(engine)
    with Session(engine) as db:
        db.add(Empresa(nombre="Empresa", sector=SectorEmpresa.MINERIA))
        db.commit()
        db.add(Empresa(nombre=" EMPRESA ", sector=SectorEmpresa.ENERGIA))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    engine.dispose()


def test_ddl_postgresql_tipos_y_unicidad():
    dialecto = postgresql.dialect()
    ddl = str(CreateTable(Empresa.__table__).compile(dialect=dialecto))
    assert "UUID" in ddl
    assert "TIMESTAMP WITH TIME ZONE" in ddl
    assert "sector_empresa" in ddl
    indice = next(iter(Empresa.__table__.indexes))
    sql = str(CreateIndex(indice).compile(dialect=dialecto))
    assert "UNIQUE INDEX uq_empresas_nombre_normalizado" in sql
    assert "lower(trim(nombre))" in sql


def test_router_integrado_en_aplicacion():
    from app.main import app

    rutas = {ruta.path for ruta in app.routes}
    assert "/empresas" in rutas
    assert "/empresas/{empresa_id}/desactivar" in rutas
