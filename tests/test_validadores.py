"""Validadores específicos de entrada y límites REALES de la biblioteca de contraseñas.

Las pruebas con ``passlib`` documentan el comportamiento del que depende la corrección: si una actualización lo cambia, fallan aquí y no
como un 500 en producción.
"""
import pytest
from passlib.exc import PasswordSizeError, PasswordValueError
from pydantic import ValidationError

from app.core import security
from app.core.security import (
    LONGITUD_MAXIMA_PASSWORD,
    hash_password,
    password_procesable,
    validar_politica_password,
    verificar_password,
    verificar_token_recuperacion,
)
from app.core.validadores import LONGITUD_MAXIMA_NOMBRE, sin_caracteres_de_control, sin_marcado_html
from app.schemas import CrearEmpresaRequest, CrearUsuarioRequest, EditarEmpresaRequest, EmpresaResponse

NUL = chr(0)
SUSTITUTO = chr(0xD800)


# ------------------------------------------------------------------------------------------------------------ validadores
@pytest.mark.parametrize("valor", [NUL, "a" + NUL, "a\tb", "a\nb", "a\rb", "a\x7fb", "a\x85b", "a\x07", SUSTITUTO, "ok" + SUSTITUTO])
def test_sin_caracteres_de_control_rechaza(valor):
    with pytest.raises(ValueError):
        sin_caracteres_de_control(valor)


@pytest.mark.parametrize("valor", ["Peña", "Ñandú", "O'Brien", "A & B", "S.A.C. (Perú), Inc.", "日本語", "emoji \U0001F600", "a b", "a b", "​"])
def test_sin_caracteres_de_control_acepta_texto_legitimo_sin_alterarlo(valor):
    assert sin_caracteres_de_control(valor) == valor


@pytest.mark.parametrize("valor", ["<", ">", "a<b", "<script>", "a>b"])
def test_sin_marcado_html_rechaza(valor):
    with pytest.raises(ValueError):
        sin_marcado_html(valor)


@pytest.mark.parametrize("valor", ["A & B", "O'Brien", "Perú, S.A.", 'Mina "La Esperanza"', "a/b", "(x) [y] {z}", "50%"])
def test_sin_marcado_html_acepta_puntuacion_legitima(valor):
    assert sin_marcado_html(valor) == valor


def test_las_reglas_son_especificas_de_cada_campo():
    # El nombre de una cuenta no rechaza «<» «>» (no estaba aprobado); el de una empresa sí. El correo y los demás campos no cambian.
    assert CrearUsuarioRequest(nombre="Ana <3", correo="a@igualab-prueba.org", password="x").nombre == "Ana <3"
    with pytest.raises(ValidationError):
        CrearEmpresaRequest(nombre="Ana <3", sector="ENERGIA")


def test_longitudes_de_columna():
    assert LONGITUD_MAXIMA_NOMBRE == 200
    CrearUsuarioRequest(nombre="x" * 200, correo="a@igualab-prueba.org", password="x")
    with pytest.raises(ValidationError):
        CrearUsuarioRequest(nombre="x" * 201, correo="a@igualab-prueba.org", password="x")
    with pytest.raises(ValidationError):
        CrearEmpresaRequest(nombre="x" * 201, sector="ENERGIA")


def test_el_nombre_de_empresa_sigue_recortando_espacios_como_antes_y_no_acepta_vacio():
    assert CrearEmpresaRequest(nombre="  Empresa Sur  ", sector="MINERIA").nombre == "Empresa Sur"
    with pytest.raises(ValidationError):
        CrearEmpresaRequest(nombre="   ", sector="MINERIA")
    assert EditarEmpresaRequest(nombre="  Nueva  ").nombre == "Nueva"
    with pytest.raises(ValidationError):
        EditarEmpresaRequest(nombre=None)  # sigue siendo un error: «no pueden ser nulos»
    assert EditarEmpresaRequest(sector="MINERIA").nombre is None


def test_la_respuesta_no_valida_ni_altera_nombres_almacenados():
    # Los validadores son solo de ENTRADA: un nombre heredado con etiquetas se serializa tal cual.
    from datetime import datetime, timezone
    import uuid

    r = EmpresaResponse(id=uuid.uuid4(), nombre="<b>Heredada</b>" + "\t", sector="MINERIA", activa=True, creada_en=datetime.now(timezone.utc))
    assert r.nombre == "<b>Heredada</b>\t"


def test_los_errores_no_reflejan_la_entrada():
    with pytest.raises(ValidationError) as e:
        CrearEmpresaRequest(nombre="<script>alert(1)</script>", sector="ENERGIA")
    assert "script" not in e.value.errors()[0]["msg"]


# ------------------------------------------------------------------------------------- límites reales de la biblioteca de hash
def test_el_tope_documentado_es_el_de_passlib():
    from passlib.utils import MAX_PASSWORD_SIZE

    assert LONGITUD_MAXIMA_PASSWORD == MAX_PASSWORD_SIZE == 4096


def test_passlib_se_comporta_como_asume_la_correccion():
    """Si esto cambia en una actualización de passlib, la lógica de ``password_procesable`` debe revisarse."""
    hash_valido = hash_password("Valida-1!")
    with pytest.raises(PasswordValueError):
        security._pwd_context.verify("a" + NUL + "b", hash_valido)
    with pytest.raises(PasswordSizeError):
        security._pwd_context.verify("A" * (LONGITUD_MAXIMA_PASSWORD + 1), hash_valido)
    with pytest.raises(UnicodeEncodeError):
        security._pwd_context.verify(SUSTITUTO, hash_valido)
    assert security._pwd_context.verify("A" * LONGITUD_MAXIMA_PASSWORD, hash_password("A" * LONGITUD_MAXIMA_PASSWORD))


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("Valida-1!", True),
        ("ñ" * 1000, True),
        ("A" * LONGITUD_MAXIMA_PASSWORD, True),
        ("A" * (LONGITUD_MAXIMA_PASSWORD + 1), False),
        ("a" + NUL + "b", False),
        (NUL, False),
        (SUSTITUTO, False),
        ("ok" + SUSTITUTO + "ok", False),
        ("\t espacios y tabs \n", True),
        ("", True),
    ],
)
def test_password_procesable(valor, esperado):
    assert password_procesable(valor) is esperado


def test_verificar_password_valida_sigue_funcionando_y_no_confunde_una_incorrecta():
    h = hash_password("Valida-1!")
    assert verificar_password("Valida-1!", h) is True
    assert verificar_password("otra-clave", h) is False


@pytest.mark.parametrize("valor", ["a" + NUL + "b", "A" * (LONGITUD_MAXIMA_PASSWORD + 1), SUSTITUTO])
def test_verificar_password_no_procesable_es_falso_sin_llamar_a_la_biblioteca(valor, monkeypatch):
    def no_debe_llamarse(*_a, **_k):
        raise AssertionError("no se debe invocar a passlib con un texto que no puede ser una contraseña")

    monkeypatch.setattr(security._pwd_context, "verify", no_debe_llamarse)
    assert verificar_password(valor, "cualquier-cosa") is False


def test_verificar_password_NO_oculta_otros_fallos(monkeypatch):
    h = hash_password("Valida-1!")
    with pytest.raises(ValueError):  # hash corrupto: es un problema de datos, no «credenciales incorrectas»
        verificar_password("Valida-1!", "esto-no-es-un-hash")

    def roto(*_a, **_k):
        raise RuntimeError("fallo de infraestructura")

    monkeypatch.setattr(security._pwd_context, "verify", roto)
    with pytest.raises(RuntimeError):
        verificar_password("Valida-1!", h)


def test_verificar_token_de_recuperacion_no_procesable_es_falso():
    token = security.generar_token_recuperacion()
    h = security.hash_token_recuperacion(token)
    assert verificar_token_recuperacion(token, h) is True
    for malo in ("a" + NUL + "b", "T" * (LONGITUD_MAXIMA_PASSWORD + 1), SUSTITUTO):
        assert verificar_token_recuperacion(malo, h) is False


# ------------------------------------------------------------------------------------------------------ política vigente
def test_la_politica_vigente_no_cambia_para_contrasenas_normales():
    assert validar_politica_password("Clave-Valida-1!", "a@b.co") == []
    assert "al menos 8" in validar_politica_password("Aa1!", "a@b.co")[0]
    assert validar_politica_password("Aa1!bbbb", "aa1!bbbb") != [], "sigue sin poder coincidir con el correo"


def test_la_politica_informa_lo_que_la_biblioteca_no_puede_procesar():
    assert any("nulos" in m for m in validar_politica_password("Aa1!" + NUL + "xyz", "a@b.co"))
    assert any("no puede superar 4096" in m for m in validar_politica_password("Aa1!" + "x" * 5000, "a@b.co"))
    assert any("no válidos" in m for m in validar_politica_password("Aa1!" + SUSTITUTO + "xyz", "a@b.co"))
    # Una contraseña de exactamente 4096 caracteres que cumple todo lo demás sigue siendo válida.
    assert validar_politica_password("Aa1!" + "x" * (LONGITUD_MAXIMA_PASSWORD - 4), "a@b.co") == []
