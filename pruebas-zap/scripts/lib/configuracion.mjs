/**
 * Lectura y validación de la configuración. Falla con un mensaje claro y un código propio; nunca imprime valores secretos
 * (solo el NOMBRE de la variable que falta). Las variables reutilizan los nombres del ejecutor de pruebas-http.
 */
import { randomBytes } from 'node:crypto';
import { ErrorDeEjecucion, SALIDA } from './codigos.mjs';

const OBLIGATORIAS = {
  IGUALAB_API_URL: 'URL base del backend de QA, sin barra final (p. ej. http://igualab-backend-qa-backend-1:8000).',
  IGUALAB_E2E_SUPERADMIN_CORREO: 'Correo del SuperAdmin de QA (credencial IGUALAB_QA_SUPERADMIN).',
  IGUALAB_E2E_SUPERADMIN_PASSWORD: 'Contraseña VIGENTE de ese SuperAdmin (credencial IGUALAB_QA_SUPERADMIN).',
};

const MARCA_QA = /(^|[-._])qa([-._]|$)/i;
const OTROS_AMBIENTES = /(^|[-._])(uat|dev|development|prod|production|stg|stage|staging|local)([-._]|$)/i;

export function leerArgumentos(argv, opciones) {
  const resultado = {};
  for (let i = 0; i < argv.length; i += 1) {
    const actual = argv[i];
    if (!actual.startsWith('--') || !opciones.includes(actual.slice(2))) {
      throw new ErrorDeEjecucion(`Argumento desconocido: ${actual}`, SALIDA.CONFIGURACION);
    }
    i += 1;
    if (i >= argv.length) throw new ErrorDeEjecucion(`Falta el valor de ${actual}.`, SALIDA.CONFIGURACION);
    resultado[actual.slice(2)] = argv[i];
  }
  for (const nombre of opciones) {
    if (resultado[nombre] === undefined) throw new ErrorDeEjecucion(`Falta el argumento --${nombre}.`, SALIDA.CONFIGURACION);
  }
  return resultado;
}

/**
 * El destino efectivo debe ser QA. El backend no publica su ambiente, así que se exige: entorno declarado `qa`, rama `qa` en Jenkins
 * y un host que identifique a QA (o confirmación explícita del host) y que no parezca UAT, desarrollo o producción.
 */
export function validarDestinoQa(url, entorno) {
  const esCi = Boolean(entorno.JENKINS_URL || entorno.BUILD_NUMBER);
  if (entorno.IGUALAB_PRUEBAS_ENTORNO !== 'qa') {
    throw new ErrorDeEjecucion('IGUALAB_PRUEBAS_ENTORNO debe ser exactamente "qa": el escaneo activo solo se ejecuta contra QA.', SALIDA.CONFIGURACION);
  }
  if (esCi && entorno.BRANCH_NAME !== 'qa') {
    throw new ErrorDeEjecucion(`El escaneo activo solo se ejecuta en la rama qa (BRANCH_NAME=${entorno.BRANCH_NAME ?? 'sin definir'}).`, SALIDA.CONFIGURACION);
  }
  if (OTROS_AMBIENTES.test(url.hostname)) {
    throw new ErrorDeEjecucion(`El host ${url.hostname} parece de otro ambiente (UAT/desarrollo/producción). Se rechaza.`, SALIDA.CONFIGURACION);
  }
  const confirmado = (entorno.ZAP_CONFIRMO_DESTINO_QA ?? '').trim().toLowerCase() === url.hostname.toLowerCase();
  if (!MARCA_QA.test(url.hostname) && !confirmado) {
    throw new ErrorDeEjecucion(
      `El host ${url.hostname} no identifica a QA. Si es el destino correcto, defina ZAP_CONFIRMO_DESTINO_QA con ese mismo host.`,
      SALIDA.CONFIGURACION,
    );
  }
  return { host: url.hostname, porNombre: MARCA_QA.test(url.hostname), porConfirmacion: confirmado };
}

export function validarConfiguracion(entorno = process.env) {
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
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || (url.pathname && url.pathname !== '/')) {
    throw new ErrorDeEjecucion('IGUALAB_API_URL debe ser http(s)://host[:puerto] sin ruta, credenciales, consulta ni fragmento.', SALIDA.CONFIGURACION);
  }
  const destino = validarDestinoQa(url, entorno);
  const espera = Number(entorno.ZAP_ESPERA_SEGUNDOS ?? 240);
  if (!Number.isFinite(espera) || espera < 1 || espera > 1800) {
    throw new ErrorDeEjecucion('ZAP_ESPERA_SEGUNDOS debe estar entre 1 y 1800.', SALIDA.CONFIGURACION);
  }
  return {
    baseUrl: url.origin,
    destino,
    esperaSegundos: espera,
    superadminCorreo: entorno.IGUALAB_E2E_SUPERADMIN_CORREO.trim(),
    superadminPassword: entorno.IGUALAB_E2E_SUPERADMIN_PASSWORD,
  };
}

/** Contraseña sintética que cumple la política (mayúscula, minúscula, dígito, especial, >= 8) y no es adivinable. */
export const nuevaContrasenaSintetica = () => `Aa1!${randomBytes(18).toString('base64url')}`;

export function nuevoIdDeEjecucion(ahora = new Date()) {
  return `${ahora.toISOString().replace(/\D/g, '').slice(0, 14)}-${randomBytes(3).toString('hex')}`;
}

export const anioActualEnLima = (ahora = new Date()) =>
  Number(new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Lima', year: 'numeric' }).format(ahora));
