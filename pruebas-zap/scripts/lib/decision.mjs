/**
 * Veredicto final del stage. Función pura: toda la lógica de «qué código de salida corresponde» está aquí para poder probarla
 * sin Docker. Nunca devuelve éxito si falta evidencia: un reporte ausente, un hook que no escribió su resumen o un análisis activo
 * no demostrado son errores técnicos, no «sin hallazgos».
 */
import { SALIDA } from './codigos.mjs';

/**
 * @param {object} e
 * @param {number} e.prepCodigo        salida de la preparación (0 = correcta)
 * @param {boolean} e.zapEjecutado     si se llegó a lanzar el contenedor de ZAP
 * @param {number|null} e.zapCodigo    código del script oficial (0/1/2/3) o del contenedor
 * @param {boolean} e.tiempoAgotado    se superó el límite global y se detuvo el contenedor
 * @param {boolean} [e.interrumpido]   la ejecución se canceló antes de terminar (cierre de emergencia)
 * @param {{ok:boolean, motivo?:string}} e.reportes
 * @param {{ok:boolean}} e.secretos
 * @param {object|null} e.hook         resumen del hook (null = ausente)
 * @param {{ok:boolean}} e.limpieza
 * @returns {{codigo:number, motivo:string, veredicto:string}}
 */
export function decidirSalida(e) {
  const r = (codigo, veredicto, motivo) => ({ codigo, veredicto, motivo });

  if (e.prepCodigo !== 0) {
    const codigo = [SALIDA.CONFIGURACION, SALIDA.BACKEND_NO_DISPONIBLE].includes(e.prepCodigo) ? e.prepCodigo : SALIDA.ERROR_ZAP;
    return r(codigo, 'FALLO TÉCNICO', `La preparación terminó con el código ${e.prepCodigo}; ZAP no se ejecutó.`);
  }
  if (e.interrumpido) return r(SALIDA.TIEMPO_AGOTADO, 'FALLO TÉCNICO', 'La ejecución fue cancelada o interrumpida antes de terminar; solo se cerraron los datos sintéticos.');
  if (e.tiempoAgotado) return r(SALIDA.TIEMPO_AGOTADO, 'FALLO TÉCNICO', 'Se superó el límite global; se detuvo y eliminó el contenedor de ZAP de esta ejecución.');
  // Antes que el código de ZAP: si el hook abortó por alcance, ZAP sale con 3 pero la causa real es el alcance.
  if (e.hook && (e.hook.violaciones_total > 0 || e.hook.autorizacion_incorrecta_total > 0 || e.hook.violaciones_de_alcance?.length || e.hook.urls_fuera_del_destino?.length || e.hook.abortado_antes_del_ataque)) {
    return r(SALIDA.ALCANCE, 'FALLO TÉCNICO', 'Se detectaron peticiones a operaciones excluidas, a URLs fuera del destino o con la credencial de otro rol (o hacia un host/ruta sin autenticación).');
  }
  if (!e.zapEjecutado || e.zapCodigo === null || ![0, 1, 2].includes(e.zapCodigo)) {
    return r(SALIDA.ERROR_ZAP, 'FALLO TÉCNICO', `ZAP terminó con un código distinto de 0, 1 o 2 (${e.zapCodigo ?? 'sin código'}).`);
  }
  if (!e.reportes.ok) return r(SALIDA.REPORTES, 'FALLO TÉCNICO', e.reportes.motivo ?? 'Faltan reportes de ZAP o son inválidos.');
  if (!e.secretos.ok) return r(SALIDA.SECRETOS, 'FALLO TÉCNICO', 'Quedaron secretos en un artefacto; no se archivó.');
  if (!e.hook) return r(SALIDA.EVIDENCIA, 'FALLO TÉCNICO', 'El hook de ZAP no escribió su resumen: no se puede demostrar la autenticación, el alcance ni el análisis activo.');
  if (!e.hook.analisis_activo?.ejecutado) {
    return r(SALIDA.EVIDENCIA, 'FALLO TÉCNICO', 'No hay evidencia de un análisis activo ejecutado (sin escaneo activo o sin peticiones de ataque).');
  }
  if (!e.hook.cobertura_autenticada?.demostrada) {
    return r(SALIDA.EVIDENCIA, 'FALLO TÉCNICO', 'No se demostró acceso autenticado real a endpoints protegidos para todos los roles (solo 401/403 no es cobertura).');
  }
  if (!e.limpieza.ok) return r(SALIDA.LIMPIEZA, 'FALLO TÉCNICO', 'No se pudo cerrar la cuenta o la empresa sintéticas.');

  if (e.zapCodigo === 1) return r(SALIDA.REGLAS_FAIL, 'FALLIDO (reglas FAIL)', 'ZAP encontró al menos una alerta de nivel FAIL.');
  if (e.zapCodigo === 2) return r(SALIDA.ADVERTENCIAS, 'INESTABLE (advertencias)', 'ZAP encontró advertencias (WARN) sin alertas FAIL.');
  if (e.hook.analisis_activo.completo === false) {
    return r(SALIDA.ADVERTENCIAS, 'INESTABLE (análisis activo parcial)', 'Sin alertas, pero el análisis activo no completó todas las reglas (límite de tiempo): no es un análisis completo.');
  }
  return r(SALIDA.OK, 'EXITOSO', 'Sin alertas FAIL ni WARN dentro del alcance escaneado (no equivale a ausencia de vulnerabilidades).');
}
