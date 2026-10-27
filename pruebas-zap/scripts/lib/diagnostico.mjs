/**
 * Diagnóstico de operaciones de archivo. Un fallo de E/S debe decir en qué FASE ocurrió, qué OPERACIÓN y sobre qué RUTA, con qué
 * código de error y con qué identidad corre el proceso (uid/gid) frente a quién es el dueño de la carpeta. Solo rutas internas del
 * contenedor (/trabajo, /privado, /resultados): nunca contenido, tokens, contraseñas ni cuerpos de autenticación.
 */
import { statSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

import { ErrorDeEjecucion, SALIDA } from './codigos.mjs';

export const identidad = () => `uid=${process.getuid?.() ?? '?'} gid=${process.getgid?.() ?? '?'}`;

/** Propietario y modo de una carpeta, o el código de error si ni siquiera se puede leer. */
export function describirCarpeta(ruta) {
  try {
    const st = statSync(ruta);
    return `${ruta}: propietario=${st.uid}:${st.gid} modo=${(st.mode & 0o7777).toString(8)}`;
  } catch (error) {
    return `${ruta}: no accesible (${error?.code ?? error?.name ?? 'ERROR'})`;
  }
}

/** ¿Es un error de E/S del sistema de archivos (tiene código y llamada al sistema)? */
export const esErrorDeArchivo = (error) => Boolean(error && typeof error === 'object' && error.code && error.syscall);

/** Mensaje de diagnóstico para un error de archivo ya capturado. No incluye `error.message` (podría traer rutas ajenas al contenedor). */
export function mensajeDeArchivo(fase, operacion, ruta, error) {
  return `[fase: ${fase}] ${operacion} ${ruta} falló con ${error?.code ?? error?.name ?? 'ERROR'} (${identidad()}; ${describirCarpeta(dirname(ruta))})`;
}

/** Ejecuta una operación de archivo; si falla, la convierte en un ErrorDeEjecucion diagnosticable (código ARCHIVOS). */
export function conDiagnostico(fase, operacion, ruta, accion) {
  try {
    return accion();
  } catch (error) {
    if (esErrorDeArchivo(error)) throw new ErrorDeEjecucion(mensajeDeArchivo(fase, operacion, ruta, error), SALIDA.ARCHIVOS);
    throw error;
  }
}

/**
 * Comprueba ANTES de tocar el backend que cada directorio de datos se puede escribir: crea y borra un archivo de sonda. Si no, el
 * error dice exactamente cuál directorio, con qué uid/gid y quién es su dueño (sin crear cuentas ni sesiones por el camino).
 */
export function comprobarEscritura(fase, directorios) {
  for (const directorio of directorios) {
    const sonda = `${directorio}/.sonda-${process.pid}`;
    conDiagnostico(fase, 'escribir sonda en', sonda, () => {
      writeFileSync(sonda, '', { mode: 0o600 });
      unlinkSync(sonda);
    });
  }
}
