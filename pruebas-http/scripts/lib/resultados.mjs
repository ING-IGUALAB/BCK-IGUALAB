/**
 * Registro de la ejecución y reportes. Todo lo que se escribe aquí sale de campos permitidos explícitamente (nombre de la
 * petición, método, RUTA sin consulta, estado HTTP, tiempo y resultado de las aserciones): NUNCA cabeceras, cuerpos, tokens ni
 * contraseñas. Además `buscarSecretos` revisa los archivos escritos antes de darlos por buenos.
 */
import { appendFileSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

export function crearRegistro(directorio) {
  mkdirSync(directorio, { recursive: true });
  const archivo = join(directorio, 'consola.log');
  writeFileSync(archivo, '');
  const registrar = (linea = '') => {
    console.log(linea);
    appendFileSync(archivo, `${linea}\n`);
  };
  return { registrar, archivo };
}

/** Variables de la corrida que pueden publicarse (identificadores y correos sintéticos); nunca tokens ni contraseñas. */
const PUBLICABLES = /^(run_id|anio_(?:actual|b|c|futuro)|sha256_b|tamano_b)$|_(?:id|correo)$/;
const EXCLUIDAS = /^superadmin_|pwd|token/i;

export function datosCreados(variables) {
  const salida = {};
  for (const { key, value } of variables) {
    if (PUBLICABLES.test(key) && !EXCLUIDAS.test(key) && value !== undefined && value !== '') salida[key] = String(value);
  }
  return Object.fromEntries(Object.entries(salida).sort(([a], [b]) => a.localeCompare(b)));
}

export function escribirResumen({ directorio, contexto, peticiones, estadisticas, fallos, datos, veredicto }) {
  const resumen = { ...contexto, veredicto, estadisticas, fallos, peticiones, datos_sinteticos_creados: datos };
  writeFileSync(join(directorio, 'resumen.json'), `${JSON.stringify(resumen, null, 2)}\n`);
  const filas = peticiones.map((p) => {
    const marca = p.aserciones_fallidas === 0 && p.estado_http !== null ? 'OK' : 'FALLA';
    return `| ${marca} | ${p.carpeta} | ${p.nombre.replaceAll('|', '/')} | ${p.metodo} | ${p.ruta} | ${p.estado_http ?? '—'} | ${p.milisegundos} | ${p.aserciones_total - p.aserciones_fallidas}/${p.aserciones_total} |`;
  });
  const lineas = [
    `# Resultado de ${contexto.coleccion} (${contexto.entorno})`,
    '',
    `- Veredicto: **${veredicto}**`,
    `- Identificador de ejecución: \`${contexto.run_id}\``,
    `- Backend: \`${contexto.host}\``,
    `- Peticiones: ${estadisticas.peticiones_ejecutadas}/${estadisticas.peticiones_esperadas} · aserciones: ${estadisticas.aserciones_total} (fallidas: ${estadisticas.aserciones_fallidas})`,
    `- Duración: ${(estadisticas.duracion_ms / 1000).toFixed(1)} s`,
    '',
    ...(fallos.length ? ['## Fallos', '', ...fallos.map((f) => `- **${f.peticion}** — ${f.prueba}: ${f.mensaje}`), ''] : []),
    '## Peticiones',
    '',
    '| Resultado | Carpeta | Petición | Método | Ruta | HTTP | ms | Aserciones |',
    '|---|---|---|---|---|---|---|---|',
    ...filas,
    '',
    '> Este informe no contiene cabeceras, cuerpos, tokens ni contraseñas.',
    '',
  ];
  writeFileSync(join(directorio, 'resumen.md'), lineas.join('\n'));
}

/** Formas de secretos que se buscan aunque no se conozca su valor: un JWT y una cabecera `Bearer <token>`. */
const PATRONES_SECRETOS = [/eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/, /Bearer\s+[A-Za-z0-9._~+/-]{20,}/];

/** Devuelve los archivos del directorio (recursivo) que contienen un secreto conocido o con forma de token. NO devuelve los valores. */
export function buscarSecretos(directorio, secretos) {
  const utiles = [...new Set(secretos.filter((s) => typeof s === 'string' && s.length >= 8))];
  const hallazgos = [];
  const recorrer = (carpeta) => {
    for (const nombre of readdirSync(carpeta)) {
      const ruta = join(carpeta, nombre);
      if (statSync(ruta).isDirectory()) {
        recorrer(ruta);
        continue;
      }
      const contenido = readFileSync(ruta, 'utf8');
      if (utiles.some((secreto) => contenido.includes(secreto)) || PATRONES_SECRETOS.some((patron) => patron.test(contenido))) hallazgos.push(ruta);
    }
  };
  recorrer(directorio);
  return hallazgos;
}
