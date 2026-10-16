"""Parámetros FIJOS de la ingesta, en código.

Acuerdo del equipo: lo que se ajusta con la experiencia (lotes, plazos, vigencia, recuperación) vive aquí y se
cambia con un commit, no con los secretos de Jenkins. El entorno conserva solo lo que cambia entre ambientes o es
secreto: URLs de bases y servicios, credenciales, identidad/modelo/dimensión/región de OCI, bucket de MinIO y
`APP_ENV`.

`PARAMETROS_INGESTA` es la configuración por defecto. Las pruebas (y quien construya los recursos a mano) la
sustituyen pasando su propia `ParametrosIngesta` a `construir_gestor`, `config_desde_settings` o
`ProveedorEmbeddingsOCI`; no hace falta editar ningún `.env`. Los valores se validan al construirla.

Valores PROVISIONALES hasta la calibración con el proveedor real y los 50 MB (D12, D23).
"""
import math
from dataclasses import dataclass
from datetime import timedelta

# Ambientes admitidos. El ambiente de la ingesta (prefijo de los originales y filtro de TODA consulta) se DERIVA de
# `APP_ENV`: no se configura dos veces.
AMBIENTES_VALIDOS = ("development", "qa", "uat")


def ambiente_desde_app_env(valor: object) -> str:
    """`APP_ENV` exacto (`development`, `qa` o `uat`). Cualquier otro valor se rechaza sin repetirlo."""
    if not isinstance(valor, str) or valor.strip() not in AMBIENTES_VALIDOS:
        raise ValueError("APP_ENV debe ser exactamente development, qa o uat.")
    return valor.strip()


def _plazo(nombre: str, valor: object) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor) or valor <= 0:
        raise ValueError(f"{nombre} debe ser un número de segundos finito y mayor que 0.")
    return float(valor)


@dataclass(frozen=True)
class ParametrosIngesta:
    # Embeddings.
    tamano_lote: int = 16
    timeout_embeddings_segundos: float = 120.0  # por llamada al proveedor, no total
    # Vigencia RENOVABLE de una ingesta en curso (no es una duración máxima).
    vigencia: timedelta = timedelta(minutes=15)
    # Recuperación periódica y cierre ordenado.
    recuperacion_habilitada: bool = True
    intervalo_recuperacion_segundos: float = 60.0
    espera_cierre_segundos: float = 30.0
    # OCI Generative AI (plazos del SDK).
    oci_connect_timeout_segundos: float = 10.0
    oci_read_timeout_segundos: float = 60.0
    # MinIO (conexión, lectura de socket y operación completa).
    minio_connect_timeout_segundos: float = 10.0
    minio_read_timeout_segundos: float = 60.0
    minio_operation_timeout_segundos: float = 300.0

    def __post_init__(self) -> None:
        if isinstance(self.tamano_lote, bool) or not isinstance(self.tamano_lote, int) or self.tamano_lote < 1:
            raise ValueError("tamano_lote debe ser un entero de al menos 1.")
        if not isinstance(self.vigencia, timedelta) or self.vigencia <= timedelta(0):
            raise ValueError("vigencia debe ser un timedelta positivo.")
        if not isinstance(self.recuperacion_habilitada, bool):
            raise ValueError("recuperacion_habilitada debe ser booleano.")
        for nombre in (
            "timeout_embeddings_segundos",
            "intervalo_recuperacion_segundos",
            "oci_connect_timeout_segundos",
            "oci_read_timeout_segundos",
            "minio_connect_timeout_segundos",
            "minio_read_timeout_segundos",
            "minio_operation_timeout_segundos",
        ):
            _plazo(nombre, getattr(self, nombre))
        if isinstance(self.espera_cierre_segundos, bool) or not isinstance(self.espera_cierre_segundos, (int, float)) \
                or not math.isfinite(self.espera_cierre_segundos) or self.espera_cierre_segundos < 0:
            raise ValueError("espera_cierre_segundos debe ser un número de segundos finito y no negativo.")


PARAMETROS_INGESTA = ParametrosIngesta()
