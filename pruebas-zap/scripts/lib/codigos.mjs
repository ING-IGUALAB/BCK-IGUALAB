/**
 * Códigos de salida del ejecutor de ZAP. 0, 1 y 2 conservan el significado del script oficial (zap-api-scan.py); el resto son
 * errores técnicos y hacen fallar el build. Jenkins: 0 → éxito · 2 → INESTABLE · cualquier otro → fallo.
 */
export const SALIDA = {
  OK: 0,
  REGLAS_FAIL: 1, // al menos una alerta de nivel FAIL
  ADVERTENCIAS: 2, // advertencias sin FAIL (o análisis activo parcial)
  ERROR_ZAP: 3, // ZAP terminó con otro código, no arrancó o no importó la especificación
  CONFIGURACION: 4, // variable faltante, destino no permitido, OpenAPI inválido o sin clasificar
  BACKEND_NO_DISPONIBLE: 5,
  TIEMPO_AGOTADO: 6, // límite global; el contenedor de ZAP de esta ejecución se detuvo y eliminó
  REPORTES: 7, // faltan o son inválidos backend-zap.html / backend-zap.json
  SECRETOS: 8, // quedó un secreto en un artefacto que iba a archivarse
  EVIDENCIA: 9, // no se demostró el acceso autenticado, el análisis activo o el resumen del hook
  LIMPIEZA: 10, // no se pudo deshabilitar la cuenta o desactivar la empresa sintéticas
  ALCANCE: 11, // se envió una petición a una operación excluida o fuera del destino
};

export class ErrorDeEjecucion extends Error {
  constructor(mensaje, codigoSalida) {
    super(mensaje);
    this.codigoSalida = codigoSalida;
  }
}
