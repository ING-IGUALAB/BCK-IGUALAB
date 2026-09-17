
import time
import statistics
from unittest.mock import AsyncMock, MagicMock
import pytest


from app.services.auth_service import autenticar
from app.security import hash_password


REPETICIONES = 20
MARGEN_TOLERADO_SEGUNDOS = 0.05




def _fake_db_sin_usuario():
    """Simula una sesión de BD donde la consulta no encuentra ningún usuario."""
    db = MagicMock()
    resultado = MagicMock()
    resultado.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=resultado)
    return db




def _fake_db_con_usuario(password_hash: str):
    """Simula una sesión de BD donde sí existe un usuario, con este hash."""
    usuario_falso = MagicMock()
    usuario_falso.password_hash = password_hash
    usuario_falso.bloqueado_hasta = None
    usuario_falso.habilitado = True


    db = MagicMock()
    resultado = MagicMock()
    resultado.scalar_one_or_none.return_value = usuario_falso
    db.execute = AsyncMock(return_value=resultado)
    return db




@pytest.mark.asyncio
async def test_tiempo_login_no_filtra_existencia_de_cuenta():
    hash_real = hash_password("clave-correcta-del-usuario-simulado")


    tiempos_cuenta_inexistente = []
    tiempos_clave_incorrecta = []


    for _ in range(REPETICIONES):
        inicio = time.perf_counter()
        with pytest.raises(Exception):
            await autenticar(_fake_db_sin_usuario(), "no-existe@igualab.org", "cualquier-clave")
        tiempos_cuenta_inexistente.append(time.perf_counter() - inicio)


        inicio = time.perf_counter()
        with pytest.raises(Exception):
            await autenticar(_fake_db_con_usuario(hash_real), "existe@igualab.org", "clave-incorrecta")
        tiempos_clave_incorrecta.append(time.perf_counter() - inicio)


    promedio_a = statistics.mean(tiempos_cuenta_inexistente)
    promedio_b = statistics.mean(tiempos_clave_incorrecta)


    assert abs(promedio_a - promedio_b) < MARGEN_TOLERADO_SEGUNDOS, (
        f"Diferencia sospechosa: inexistente={promedio_a:.4f}s vs incorrecta={promedio_b:.4f}s"
    )
