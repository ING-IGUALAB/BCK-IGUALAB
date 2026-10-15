/**
 * Lectura y validación de la configuración del ejecutor. Falla con un mensaje claro y un código de salida propio; nunca imprime
 * valores secretos (solo el NOMBRE de la variable que falta).
 */
import { randomBytes } from 'node:crypto';

export const SALIDA = { OK: 0, PRUEBAS_FALLIDAS: 1, CONFIGURACION: 2, BACKEND_NO_DISPONIBLE: 3, GUARDA: 4, FUGA_DE_SECRETOS: 5 };

export class ErrorDeEjecucion extends Error {
  constructor(mensaje, codigoSalida) {
    super(mensaje);
    this.codigoSalida = codigoSalida;
  }
}

const COLECCIONES = { integracion: 'igualab-integracion.postman_collection.json', e2e: 'igualab-e2e.postman_collection.json' };
const ENTORNOS = ['local', 'qa', 'uat'];

/** Variables obligatorias: se informa el NOMBRE exacto, jamás su valor. */
const OBLIGATORIAS = {
  IGUALAB_API_URL: 'URL base del backend desplegado, sin barra final (p. ej. http://igualab-backend-qa-backend-1:8000).',
  IGUALAB_E2E_SUPERADMIN_CORREO: 'Correo del SuperAdmin con el que se ejecutan las pruebas.',
  IGUALAB_E2E_SUPERADMIN_PASSWORD: 'Contraseña VIGENTE de ese SuperAdmin (no se asume que coincida con SUPERADMIN_PASSWORD del aprovisionamiento).',
};

export function leerArgumentos(argv) {
  const argumentos = { carpetas: [], espera: undefined, resultados: undefined, sinEjecutar: false };
  for (let i = 0; i < argv.length; i += 1) {
    const actual = argv[i];
    const siguiente = () => {
      i += 1;
      if (i >= argv.length) throw new ErrorDeEjecucion(`Falta el valor de ${actual}.`, SALIDA.CONFIGURACION);
      return argv[i];
    };
    if (actual === '--coleccion') argumentos.coleccion = siguiente();
    else if (actual === '--entorno') argumentos.entorno = siguiente();
    else if (actual === '--carpeta') argumentos.carpetas.push(siguiente());
    else if (actual === '--espera') argumentos.espera = Number(siguiente());
    else if (actual === '--resultados') argumentos.resultados = siguiente();
    else if (actual === '--sin-ejecutar') argumentos.sinEjecutar = true;
    else throw new ErrorDeEjecucion(`Argumento desconocido: ${actual}`, SALIDA.CONFIGURACION);
  }
  return argumentos;
}

export function validarConfiguracion(argumentos, entorno = process.env) {
  if (!Object.hasOwn(COLECCIONES, argumentos.coleccion ?? '')) {
    throw new ErrorDeEjecucion(`--coleccion debe ser una de: ${Object.keys(COLECCIONES).join(', ')}.`, SALIDA.CONFIGURACION);
  }
  const nombreEntorno = argumentos.entorno ?? entorno.IGUALAB_PRUEBAS_ENTORNO;
  if (!ENTORNOS.includes(nombreEntorno ?? '')) {
    throw new ErrorDeEjecucion(`Indique el entorno con --entorno o IGUALAB_PRUEBAS_ENTORNO (${ENTORNOS.join(' | ')}). No hay valor por defecto.`, SALIDA.CONFIGURACION);
  }
  const faltantes = Object.keys(OBLIGATORIAS).filter((nombre) => !(entorno[nombre] ?? '').trim());
  if (faltantes.length) {
    const detalle = faltantes.map((nombre) => `  - ${nombre}: ${OBLIGATORIAS[nombre]}`).join('\n');
    throw new ErrorDeEjecucion(`Faltan variables de entorno obligatorias (solo se informa su nombre):\n${detalle}`, SALIDA.CONFIGURACION);
  }
  let url;
  try {
    url = new URL(entorno.IGUALAB_API_URL.trim());
  } catch {
    throw new ErrorDeEjecucion('IGUALAB_API_URL no es una URL válida.', SALIDA.CONFIGURACION);
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new ErrorDeEjecucion('IGUALAB_API_URL debe ser http(s)://host[:puerto][/ruta] sin credenciales, consulta ni fragmento.', SALIDA.CONFIGURACION);
  }
  const base = `${url.origin}${url.pathname.replace(/\/+$/, '')}`;
  const espera = argumentos.espera ?? Number(entorno.IGUALAB_ESPERA_SEGUNDOS ?? 240);
  if (!Number.isFinite(espera) || espera < 1 || espera > 1800) {
    throw new ErrorDeEjecucion('El plazo de espera debe estar entre 1 y 1800 segundos.', SALIDA.CONFIGURACION);
  }
  protegerUat(nombreEntorno, url.hostname, entorno);
  return {
    coleccion: argumentos.coleccion,
    archivoColeccion: COLECCIONES[argumentos.coleccion],
    entorno: nombreEntorno,
    baseUrl: base,
    host: url.host,
    carpetas: argumentos.carpetas,
    esperaSegundos: espera,
    resultados: argumentos.resultados,
    sinEjecutar: argumentos.sinEjecutar,
    superadminCorreo: entorno.IGUALAB_E2E_SUPERADMIN_CORREO.trim(),
    superadminPassword: entorno.IGUALAB_E2E_SUPERADMIN_PASSWORD,
  };
}

/**
 * UAT es el entorno de la DEMOSTRACIÓN: estas colecciones ESCRIBEN datos y nunca deben correr solas contra él.
 * Solo una persona, desde su sesión, con la confirmación explícita y fuera de Jenkins/CI.
 */
function protegerUat(nombreEntorno, host, entorno) {
  const esCi = Boolean(entorno.JENKINS_URL || entorno.BUILD_NUMBER || entorno.CI);
  if (nombreEntorno === 'uat') {
    if (esCi) {
      throw new ErrorDeEjecucion('Bloqueado: no se ejecutan automáticamente estas colecciones (escriben datos) contra UAT. Ejecútelas a mano para la demostración.', SALIDA.GUARDA);
    }
    if (entorno.IGUALAB_CONFIRMO_ESCRITURA_UAT !== 'SI') {
      throw new ErrorDeEjecucion('UAT: estas colecciones crean cuentas, empresas y un documento. Para continuar defina IGUALAB_CONFIRMO_ESCRITURA_UAT=SI.', SALIDA.GUARDA);
    }
  } else if (/uat/i.test(host)) {
    throw new ErrorDeEjecucion(`El entorno declarado (${nombreEntorno}) no coincide con el host (${host}), que parece UAT. Revise IGUALAB_API_URL.`, SALIDA.GUARDA);
  }
}

/** Identificador único de ejecución en minúsculas (también forma parte de los correos sintéticos). */
export function nuevoIdDeEjecucion(ahora = new Date()) {
  const marca = ahora.toISOString().replace(/\D/g, '').slice(0, 14);
  return `${marca}-${randomBytes(3).toString('hex')}`;
}

/** Contraseña sintética que cumple la política (mayúscula, minúscula, dígito, especial, >= 8) y no es adivinable. */
export function nuevaContrasenaSintetica() {
  return `Aa1!${randomBytes(18).toString('base64url')}`;
}

/** Año actual en America/Lima (la regla de negocio acepta de 2000 al año actual en esa zona). */
export function anioActualEnLima(ahora = new Date()) {
  return Number(new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Lima', year: 'numeric' }).format(ahora));
}
