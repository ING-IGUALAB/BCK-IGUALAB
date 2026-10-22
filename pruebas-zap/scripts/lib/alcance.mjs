/**
 * Alcance del escaneo: clasificación explícita (método + ruta) de las operaciones del OpenAPI y derivación de las reglas que
 * consume el hook de ZAP. La fuente única es alcance/operaciones.json.
 */
import { ErrorDeEjecucion, SALIDA } from './codigos.mjs';

export const METODOS_HTTP = ['get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace'];

const escaparRegex = (texto) => texto.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** `/empresas/{empresa_id}` → `^/empresas/[^/]+$` (sin consulta, sin barra final obligatoria). */
export function plantillaARegex(ruta) {
  const cuerpo = ruta
    .split('/')
    .map((segmento) => (/^\{[^/{}]+\}$/.test(segmento) ? '[^/]+' : escaparRegex(segmento)))
    .join('/');
  return `^${cuerpo}/?$`;
}

export const clave = (metodo, ruta) => `${metodo.toUpperCase()} ${ruta}`;

/** Valida la forma del archivo de alcance (duplicados, decisiones, roles). */
export function validarAlcance(alcance) {
  const roles = Object.keys(alcance.roles ?? {});
  const categorias = Object.keys(alcance.categorias ?? {});
  const vistas = new Set();
  for (const op of alcance.operaciones ?? []) {
    const id = clave(op.metodo ?? '', op.ruta ?? '');
    if (vistas.has(id)) throw new ErrorDeEjecucion(`alcance/operaciones.json: operación duplicada ${id}.`, SALIDA.CONFIGURACION);
    vistas.add(id);
    if (!['incluida', 'excluida'].includes(op.decision)) throw new ErrorDeEjecucion(`alcance: ${id} sin decisión válida.`, SALIDA.CONFIGURACION);
    if (!roles.includes(op.rol)) throw new ErrorDeEjecucion(`alcance: ${id} con rol desconocido (${op.rol}).`, SALIDA.CONFIGURACION);
    if (!categorias.includes(op.categoria)) throw new ErrorDeEjecucion(`alcance: ${id} con categoría desconocida.`, SALIDA.CONFIGURACION);
    if (!String(op.motivo ?? '').trim()) throw new ErrorDeEjecucion(`alcance: ${id} sin motivo.`, SALIDA.CONFIGURACION);
  }
  return alcance;
}

/** Operaciones presentes en un OpenAPI como lista de {metodo, ruta}. */
export function operacionesDeOpenapi(especificacion) {
  const lista = [];
  for (const [ruta, item] of Object.entries(especificacion.paths ?? {})) {
    for (const metodo of METODOS_HTTP) {
      if (item && typeof item === 'object' && item[metodo]) lista.push({ metodo: metodo.toUpperCase(), ruta });
    }
  }
  return lista;
}

/**
 * Cruza el OpenAPI real con el alcance. `sinClasificar`: operaciones nuevas que nadie decidió (la preparación falla);
 * `ausentes`: clasificadas pero que ya no existen (solo se informa).
 */
export function clasificar(especificacion, alcance) {
  const reales = operacionesDeOpenapi(especificacion);
  const decididas = new Map(alcance.operaciones.map((op) => [clave(op.metodo, op.ruta), op]));
  const incluidas = [];
  const excluidas = [];
  const sinClasificar = [];
  for (const real of reales) {
    const op = decididas.get(clave(real.metodo, real.ruta));
    if (!op) sinClasificar.push(real);
    else (op.decision === 'incluida' ? incluidas : excluidas).push(op);
  }
  const presentes = new Set(reales.map((o) => clave(o.metodo, o.ruta)));
  const ausentes = alcance.operaciones.filter((op) => !presentes.has(clave(op.metodo, op.ruta)));
  return { incluidas, excluidas, sinClasificar, ausentes };
}

/**
 * Configuración pública para el hook de ZAP (no contiene secretos): reglas de inyección de credenciales por rol, operaciones
 * excluidas (para vigilar que ninguna se haya enviado) y rutas con TODOS sus métodos excluidos (exclusión adicional del ataque).
 */
export function configuracionDelHook({ baseUrl, incluidas, excluidas, minimos = 1 }) {
  const origen = new URL(baseUrl).origin;
  const escapadoOrigen = escaparRegex(origen);
  const rolesUsados = [...new Set(incluidas.map((op) => op.rol).filter((rol) => rol !== 'publico'))].sort();
  const roles = {};
  for (const rol of rolesUsados) {
    // Un patrón por rol: primer segmento de la ruta (prefijo), para que también cubra las variantes que genera el ataque activo.
    const prefijos = [...new Set(incluidas.filter((op) => op.rol === rol).map((op) => op.ruta.split('/')[1]))].sort();
    roles[rol] = { url_regex: `^${escapadoOrigen}/(${prefijos.map(escaparRegex).join('|')})([/?].*)?$`, prefijos };
  }
  const rutasIncluidas = new Set(incluidas.map((op) => op.ruta));
  const soloExcluidas = [...new Set(excluidas.map((op) => op.ruta))].filter((ruta) => !rutasIncluidas.has(ruta));
  const urlDeRuta = (ruta) => `^${escapadoOrigen}${plantillaARegex(ruta).slice(1, -3)}/?(\\?.*)?$`;
  // Barrera que IMPIDE (no solo detecta) la ejecución de una operación excluida: ZAP cambia el método de las peticiones, así que una ruta
  // con un GET incluido y un POST excluido recibiría ese POST. Replacer lo reescribe a OPTIONS (405, sin ejecutar nada). En las rutas con
  // todos sus métodos excluidos se reescribe cualquier método.
  const neutralizar = [];
  for (const ruta of new Set(excluidas.map((op) => op.ruta))) {
    const metodos = [...new Set(excluidas.filter((op) => op.ruta === ruta).map((op) => op.metodo))];
    const compartida = rutasIncluidas.has(ruta);
    neutralizar.push({
      ruta,
      metodos: compartida ? metodos : ['*'],
      metodo_regex: compartida ? `^(${metodos.join('|')}) ` : '^[A-Z]+ ',
      url_regex: urlDeRuta(ruta),
    });
  }
  // Ninguna credencial fuera de las rutas de un rol: ni hacia otro host ni hacia rutas públicas o de sondeo del propio backend.
  const prefijosDeRoles = [...new Set(Object.values(roles).flatMap((r) => r.prefijos))].sort();
  const sinCredencial = `^(?!${escapadoOrigen}/(${prefijosDeRoles.map(escaparRegex).join('|')})([/?].*)?$).*`;
  return {
    version: 1,
    destino: origen,
    roles,
    minimos_autenticados_por_rol: Object.fromEntries(rolesUsados.map((rol) => [rol, minimos])),
    incluidas: incluidas.map((op) => ({ metodo: op.metodo, ruta: op.ruta, rol: op.rol, regex: plantillaARegex(op.ruta) })),
    excluidas: excluidas.map((op) => ({ metodo: op.metodo, ruta: op.ruta, categoria: op.categoria, regex: plantillaARegex(op.ruta) })),
    excluir_del_ataque: soloExcluidas.map(urlDeRuta),
    neutralizar_metodos: neutralizar,
    url_sin_credencial_regex: sinCredencial,
  };
}
