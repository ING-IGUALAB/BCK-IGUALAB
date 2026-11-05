"""Exclusión mutua transaccional del ORIGEN de un análisis GRI (BUG-15, entrega 1).

QUÉ PROTEGE. El origen validado de una empresa/año es el conjunto de documentos elegibles (completados, con análisis y con
`vector_publicado_en` confirmado, de una empresa activa), la versión de estados y la validación. Quien lo lee para decidir algo
(validar, emitir un reporte) y quien lo modifica (asignar un estado, confirmar la publicación vectorial de un documento, cambiar
la actividad de la empresa) deben excluirse entre sí hasta el FIN de sus transacciones, o una emisión puede confirmarse sobre un
origen que cambió un instante antes.

CÓMO. `proteger_origen_de_analisis` toma, en este orden y siempre dentro de la transacción del llamador:
1. la fila de la EMPRESA en modo compartido (`FOR SHARE`). Dos empresas, o dos años de una misma empresa, no se bloquean entre sí.
   Es incompatible con `FOR NO KEY UPDATE`, que toma quien cambia la actividad o el sector de la empresa
   (`empresas.service.obtener_empresa_bloqueada(para_modificar=True)`): una desactivación, que afecta a TODOS los años de la
   empresa, espera a las operaciones en curso y las posteriores la ven aplicada. No hace falta enumerar años ni filas.
2. la fila de `analisis_gri_control` de (ambiente, empresa, año) con `FOR UPDATE`, creándola si falta. Es el mutex compartido por
   asignaciones de estado, validaciones, emisiones y confirmaciones de publicación del mismo año.

ORDEN ÚNICO DE ADQUISICIÓN en todos los caminos (evita bloqueos mutuos): empresa (compartido) → control (exclusivo) → [candado
asesor de numeración del reporte, solo en la emisión] → filas de `documentos`/`reportes_prospeccion` que se escriben. Ningún camino
toma una fila de documento y después la empresa o el control. Un bloqueo exclusivo de empresa (`FOR NO KEY UPDATE`) se toma sin
tener ningún otro bloqueo de esta lista.

REGLAS. No hace commit ni rollback ni abre sesiones: el llamador confirma o deshace y sus bloqueos duran hasta entonces. No debe
llamarse durante renderizados de PDF ni llamadas a OCI, MinIO o a la base vectorial. No usa candados en memoria: funciona entre
procesos y entre instancias porque el candado es de PostgreSQL. Este módulo solo depende de modelos para que ingesta y análisis
puedan usarlo sin dependencias circulares.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models import Empresa
from app.models.analisis_gri import AnalisisGriControl


async def proteger_origen_de_analisis(db: AsyncSession, *, ambiente: str, empresa_id: uuid.UUID, anio: int) -> int:
    """Adquiere la protección común del origen de (ambiente, empresa, año) hasta el fin de la transacción de `db` y devuelve la
    versión de estados (`analisis_gri_control.version`) vista bajo el bloqueo. `NotFoundError` si la empresa no existe."""
    empresa = await db.execute(select(Empresa.id).where(Empresa.id == empresa_id).with_for_update(read=True))
    if empresa.first() is None:
        raise NotFoundError("COMPANY_NOT_FOUND", "La empresa indicada no existe.")
    await db.execute(
        pg_insert(AnalisisGriControl)
        .values(ambiente=ambiente, empresa_id=empresa_id, anio=anio)
        .on_conflict_do_nothing(index_elements=["ambiente", "empresa_id", "anio"])
    )
    version = await db.execute(
        select(AnalisisGriControl.version)
        .where(
            AnalisisGriControl.ambiente == ambiente,
            AnalisisGriControl.empresa_id == empresa_id,
            AnalisisGriControl.anio == anio,
        )
        .with_for_update()
    )
    return version.scalar_one()
