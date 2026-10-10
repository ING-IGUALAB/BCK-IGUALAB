import enum


# LTX:RN-020. Solo el enum: la tabla `documentos` depende de D03/D05/D10/D17 (Etapa 4).
class TipoDocumento(str, enum.Enum):
    MEMORIA_ANUAL = "MEMORIA_ANUAL"
    REPORTE_SOSTENIBILIDAD_GRI = "REPORTE_SOSTENIBILIDAD_GRI"
