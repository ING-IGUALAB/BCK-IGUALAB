/**
 * Resolución de la selección de carpetas (`--carpeta`) ANTES de enviar ninguna petición.
 *
 * Reglas (las declara la propia colección, en dos variables JSON que viajan en el archivo versionado):
 *  - `requisitos_carpetas`: {"C": ["B"], ...}  la carpeta de la letra de la izquierda necesita los datos de las de la derecha, que deben
 *    formar parte de la MISMA selección. El ejecutor no reutiliza IDs ni tokens de otra ejecución (cada corrida parte de un entorno
 *    vacío), así que una selección sin sus requisitos falla aquí, con un mensaje claro, en lugar de fallar a mitad de la corrida.
 *  - `cierres_carpetas`: {"B": "Z"}  si la selección incluye B se añade Z AL FINAL: es el cierre lógico de los datos de B y debe ir
 *    después de todos sus consumidores.
 *
 * Newman ejecuta las carpetas en el orden de la COLECCIÓN, no en el que se pasan en la línea de comandos; aquí se devuelven ya en ese
 * orden para que lo que se informa sea lo que se ejecuta.
 */
import { ErrorDeEjecucion, SALIDA } from './configuracion.mjs';

const falla = (mensaje) => new ErrorDeEjecucion(mensaje, SALIDA.CONFIGURACION);

/** «B. Ingesta…» → «B». Un nombre sin «. » se usa completo. */
export const letraDeCarpeta = (nombre) => {
  const posicion = nombre.indexOf('. ');
  return posicion === -1 ? nombre : nombre.slice(0, posicion);
};

function leerMapa(coleccion, nombre) {
  const variable = (coleccion.variable ?? []).find((v) => v.key === nombre);
  if (!variable) return {};
  try {
    const mapa = JSON.parse(variable.value);
    if (mapa === null || typeof mapa !== 'object' || Array.isArray(mapa)) throw new Error('no es un objeto');
    return mapa;
  } catch {
    throw falla(`La variable «${nombre}» de la colección no es un objeto JSON válido; regenere las colecciones (npm run generar).`);
  }
}

/** `--carpeta B` resuelve a la carpeta de primer nivel cuyo nombre empieza por «B. » (o coincide exactamente). */
function resolverNombres(carpetas, pedidas) {
  const nombres = carpetas.map((n) => n.name);
  return pedidas.map((pedida) => {
    const coincidencias = nombres.filter((n) => n === pedida || n.startsWith(`${pedida}. `) || n.startsWith(`${pedida} `));
    if (coincidencias.length !== 1) {
      throw falla(`--carpeta «${pedida}» no identifica una única carpeta. Disponibles: ${nombres.join(' | ')}`);
    }
    return coincidencias[0];
  });
}

/**
 * @returns {{carpetas: string[], agregadas: string[]}} `carpetas` en orden de colección (vacío = colección completa);
 *          `agregadas` son las carpetas de cierre que el ejecutor añadió por su cuenta.
 */
export function resolverSeleccion(coleccion, pedidas) {
  if (pedidas.length === 0) return { carpetas: [], agregadas: [] };
  const carpetas = coleccion.item.filter((n) => !n.request);
  const requisitos = leerMapa(coleccion, 'requisitos_carpetas');
  const cierres = leerMapa(coleccion, 'cierres_carpetas');
  const porLetra = new Map(carpetas.map((c) => [letraDeCarpeta(c.name), c.name]));

  const elegidas = new Set(resolverNombres(carpetas, pedidas));
  const letrasElegidas = () => new Set([...elegidas].map(letraDeCarpeta));

  // Requisitos primero: así «Z» sola se rechaza por no incluir a B en lugar de aceptarse por error.
  const faltan = [];
  for (const letra of letrasElegidas()) {
    for (const requerida of requisitos[letra] ?? []) {
      if (!letrasElegidas().has(requerida)) faltan.push(`${letra} necesita los datos de ${requerida}`);
    }
  }
  if (faltan.length) {
    throw falla(
      `La selección no es ejecutable (${faltan.join('; ')}). Cada ejecución crea sus propios datos y no reutiliza los de otra: ` +
      'incluya también esas carpetas, por ejemplo «--carpeta B --carpeta C».',
    );
  }

  const agregadas = [];
  for (const [origen, cierre] of Object.entries(cierres)) {
    const nombreCierre = porLetra.get(cierre);
    if (!nombreCierre) throw falla(`La colección declara el cierre «${cierre}» de «${origen}», pero esa carpeta no existe.`);
    if (letrasElegidas().has(origen) && !elegidas.has(nombreCierre)) {
      elegidas.add(nombreCierre);
      agregadas.push(nombreCierre);
    }
  }
  return { carpetas: carpetas.filter((c) => elegidas.has(c.name)).map((c) => c.name), agregadas };
}
