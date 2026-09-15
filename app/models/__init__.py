"""
Punto único de import para todos los modelos del sistema.

Gracias a esto, el resto del código (servicios, routers, scripts) sigue
escribiendo:

    from app.models import Usuario, RolUsuario, TipoEventoAuditoria

exactamente igual que cuando todo vivía en un solo archivo `models.py` —
nadie fuera de esta carpeta necesita saber en qué archivo específico vive
cada tabla.

Cuando agreguen un módulo nuevo (ej. Ingesta), el patrón a seguir es:
  1. Crear `app/models/documentos.py` con sus clases (Documento, Empresa).
  2. Agregar la línea de import correspondiente aquí abajo.
No hay que tocar ningún otro archivo del proyecto para que el resto del
código pueda usar las tablas nuevas.
"""
from app.models.usuarios import Usuario, Sesion, TokenRecuperacion, RolUsuario
from app.models.auditoria import RegistroAuditoria, TipoEventoAuditoria

# Futuros módulos (descomentar cuando se creen los archivos correspondientes):
# from app.models.documentos import Documento, Empresa
# from app.models.analisis import CodigoGriReportado, Sancion
# from app.models.reportes import ReporteProspeccion

__all__ = [
    "Usuario", "Sesion", "TokenRecuperacion", "RolUsuario",
    "RegistroAuditoria", "TipoEventoAuditoria",
]
