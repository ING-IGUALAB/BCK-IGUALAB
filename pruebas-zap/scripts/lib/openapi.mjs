/**
 * Validación del OpenAPI REAL publicado por la aplicación (que nunca se modifica) y derivación de la especificación filtrada que
 * se entrega a ZAP. ZAP, al importar, ENVÍA una petición por operación de la especificación: por eso las operaciones excluidas
 * se retiran de la especificación y no solo del ataque activo.
 */
import { ErrorDeEjecucion, SALIDA } from './codigos.mjs';
import { METODOS_HTTP } from './alcance.mjs';

const CAMPOS_URL_DE_SEGURIDAD = ['tokenUrl', 'authorizationUrl', 'refreshUrl', 'openIdConnectUrl'];

function recorrer(nodo, visitar, ruta = '') {
  if (Array.isArray(nodo)) nodo.forEach((hijo, i) => recorrer(hijo, visitar, `${ruta}[${i}]`));
  else if (nodo && typeof nodo === 'object') {
    for (const [clave, valor] of Object.entries(nodo)) {
      visitar(clave, valor, ruta);
      recorrer(valor, visitar, `${ruta}/${clave}`);
    }
  }
}

/** Una URL de servidor es aceptable si es relativa o tiene exactamente el origen del destino. */
function servidorPermitido(url, origen) {
  if (typeof url !== 'string') return false;
  if (url.startsWith('/') && !url.startsWith('//')) return true;
  try {
    return new URL(url).origin === origen;
  } catch {
    return false;
  }
}

/**
 * Problemas que hacen insegura la importación: servidores de otro ambiente, `$ref` remotas (el importador de ZAP las
 * resolvería por red) o URLs de esquemas de seguridad hacia otro origen. Devuelve una lista de textos (vacía = correcto).
 */
export function problemasDeDestino(especificacion, baseUrl) {
  const origen = new URL(baseUrl).origin;
  const problemas = [];
  recorrer(especificacion, (clave, valor, ruta) => {
    if (clave === 'servers' && Array.isArray(valor)) {
      for (const servidor of valor) {
        if (!servidorPermitido(servidor?.url, origen)) problemas.push(`servers en ${ruta || '/'} apunta a otro destino (${servidor?.url})`);
      }
    }
    if (clave === '$ref' && typeof valor === 'string' && !valor.startsWith('#')) problemas.push(`$ref no local en ${ruta}: ${valor}`);
    if (CAMPOS_URL_DE_SEGURIDAD.includes(clave) && typeof valor === 'string' && /^https?:/i.test(valor) && !servidorPermitido(valor, origen)) {
      problemas.push(`${clave} en ${ruta} apunta a otro destino (${valor})`);
    }
  });
  return problemas;
}

export function validarEstructura(especificacion) {
  if (!especificacion || typeof especificacion !== 'object' || Array.isArray(especificacion)) {
    throw new ErrorDeEjecucion('/openapi.json no es un objeto JSON.', SALIDA.CONFIGURACION);
  }
  if (!/^3\.\d+(\.\d+)?/.test(String(especificacion.openapi ?? ''))) {
    throw new ErrorDeEjecucion(`/openapi.json no declara OpenAPI 3.x (campo openapi: ${JSON.stringify(especificacion.openapi)}).`, SALIDA.CONFIGURACION);
  }
  const rutas = especificacion.paths;
  if (!rutas || typeof rutas !== 'object' || Object.keys(rutas).length === 0) {
    throw new ErrorDeEjecucion('/openapi.json no contiene ninguna ruta (paths vacío).', SALIDA.CONFIGURACION);
  }
}

const UUID_INEXISTENTE = '00000000-0000-4000-8000-000000000000';

/** Identidad INEXISTENTE para el escaneo de /auth/login: ninguna cuenta real puede bloquearse. */
export const IDENTIDAD_LOGIN_DE_ESCANEO = {
  correo: 'zap-escaner-sin-cuenta@igualab-zap.example.com',
  password: 'Zap-sin-cuenta-1!',
};

function ejemplo(esquema, valor) {
  esquema.example = valor;
  esquema.examples = [valor];
}

/**
 * Especificación filtrada: solo las operaciones incluidas, un único servidor (el destino) y valores de ejemplo que dirigen las
 * peticiones al recurso sintético de esta ejecución. Devuelve una copia; el original no cambia.
 */
export function especificacionFiltrada(original, { baseUrl, incluidas, empresaId, anio }) {
  const copia = structuredClone(original);
  const permitidas = new Set(incluidas.map((op) => `${op.metodo} ${op.ruta}`));
  const rutas = {};
  for (const [ruta, item] of Object.entries(copia.paths)) {
    const nuevo = {};
    for (const [clave, valor] of Object.entries(item)) {
      if (!METODOS_HTTP.includes(clave)) nuevo[clave] = valor; // parámetros comunes, summary…
      else if (permitidas.has(`${clave.toUpperCase()} ${ruta}`)) nuevo[clave] = valor;
    }
    if (METODOS_HTTP.some((metodo) => nuevo[metodo])) rutas[ruta] = nuevo;
  }
  copia.paths = rutas;
  delete copia.servers;
  for (const item of Object.values(rutas)) {
    delete item.servers;
    for (const metodo of METODOS_HTTP) if (item[metodo]) delete item[metodo].servers;
  }
  copia.servers = [{ url: new URL(baseUrl).origin }];

  const ejemplosDeParametro = {
    empresa_id: empresaId,
    anio: String(anio),
    documento_id: UUID_INEXISTENTE,
    operacion_id: UUID_INEXISTENTE,
    reporte_id: UUID_INEXISTENTE,
  };
  for (const item of Object.values(rutas)) {
    for (const metodo of METODOS_HTTP) {
      for (const parametro of item[metodo]?.parameters ?? []) {
        if (Object.hasOwn(ejemplosDeParametro, parametro.name)) {
          parametro.example = ejemplosDeParametro[parametro.name];
          parametro.schema = { ...(parametro.schema ?? {}), example: ejemplosDeParametro[parametro.name] };
        }
      }
    }
  }
  for (const [nombre, esquema] of Object.entries(copia.components?.schemas ?? {})) {
    const propiedades = esquema.properties ?? {};
    if (propiedades.empresa_id) ejemplo(propiedades.empresa_id, empresaId);
    if (propiedades.anio && nombre !== 'MetadatosIngestaRequest') ejemplo(propiedades.anio, anio);
    if (nombre === 'LoginRequest') {
      if (propiedades.correo) ejemplo(propiedades.correo, IDENTIDAD_LOGIN_DE_ESCANEO.correo);
      if (propiedades.password) ejemplo(propiedades.password, IDENTIDAD_LOGIN_DE_ESCANEO.password);
    }
  }
  return copia;
}
