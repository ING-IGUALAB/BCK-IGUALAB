/**
 * Mini-DSL para escribir colecciones Postman (formato v2.1) como código legible.
 *
 * Las aserciones se escriben como FUNCIONES JavaScript reales: Node valida su sintaxis al generar y el generador las serializa
 * con `toString()` dentro del script de la petición. Se ejecutan en el sandbox de Postman/Newman, no en Node, de modo que los
 * identificadores `pm`, `j`, `v`, `set`… solo existen allí (ver PRELUDIO).
 *
 * Convenciones de seguridad (la colección NUNCA debe filtrar secretos):
 *  - No se compara ni se imprime ningún token ni contraseña en una aserción (un mensaje de chai incluiría el valor).
 *  - No hay `console.log`: Newman no imprime cuerpos ni cabeceras y el ejecutor tampoco los guarda.
 */

/** Código común que se antepone a TODO script de pruebas. Es JavaScript del sandbox de Postman. */
export const PRELUDIO = `
const j = (() => { try { return pm.response.json(); } catch (e) { return null; } })();
const v = (nombre) => pm.variables.get(nombre);
const set = (nombre, valor) => { if (valor !== undefined && valor !== null) { pm.collectionVariables.set(nombre, String(valor)); if (!/token|pwd|password/i.test(nombre)) { pm.environment.set(nombre, String(valor)); } } };
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const esUuid = (x) => typeof x === 'string' && UUID.test(x);
const esFecha = (x) => typeof x === 'string' && !Number.isNaN(Date.parse(x));
const esHex64 = (x) => typeof x === 'string' && /^[0-9a-f]{64}$/.test(x);
const esJwt = (x) => typeof x === 'string' && x.split('.').length === 3 && x.length > 40;
const sinSecretos = (cuerpo) => !/password|passwd|contrase|secret|hash/i.test(Object.keys(cuerpo || {}).join(','));
const errorContrato = (estado, codigo) => {
  pm.test('HTTP ' + estado, () => pm.response.to.have.status(estado));
  pm.test('error ' + codigo + ' con el contrato {error:{code,message,details,request_id}}', () => {
    pm.expect(j, 'el cuerpo debe ser JSON').to.be.an('object');
    pm.expect(Object.keys(j)).to.have.members(['error']);
    pm.expect(Object.keys(j.error)).to.have.members(['code', 'message', 'details', 'request_id']);
    pm.expect(j.error.code).to.equal(codigo);
    pm.expect(j.error.message).to.be.a('string').and.not.empty;
    pm.expect(esUuid(j.error.request_id), 'request_id es un UUID').to.equal(true);
    pm.expect(pm.response.headers.get('X-Request-ID'), 'la cabecera X-Request-ID coincide con el cuerpo').to.equal(j.error.request_id);
  });
};
`;

const VERSION_SCHEMA = 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json';

const aScript = (lineas) => ({ type: 'text/javascript', exec: lineas.join('\n').split('\n') });

/** Serializa una función como código ejecutable: `(function)(argumentos);` */
const invocar = (fn, argumentos) => `(${fn.toString()})(${argumentos});`;

/** Generadores de código de pruebas (cada uno devuelve un trozo de JavaScript para el sandbox). */
export const T = {
  /** `pm.test` con una función `(j, v) => { ...pm.expect... }`. */
  prueba(nombre, fn, args = {}) {
    // `args` viaja como literal JSON: una función serializada NO puede capturar variables del generador.
    return `pm.test(${JSON.stringify(nombre)}, () => { ${invocar(fn, 'j, v, ' + JSON.stringify(args))} });`;
  },
  estado(codigo) {
    return `pm.test('HTTP ${codigo}', () => pm.response.to.have.status(${codigo}));`;
  },
  /** Error de la aplicación con el contrato real; `detalles` opcional: `(details, v) => {...}`. */
  error(estado, codigo, detalles) {
    const base = `errorContrato(${estado}, ${JSON.stringify(codigo)});`;
    if (!detalles) return base;
    return `${base}\npm.test('detalles de ${codigo}', () => { ${invocar(detalles, 'j.error.details, v')} });`;
  },
  /** Error de validación 422: contrato real de `REQUEST_VALIDATION_ERROR`; `ubicacion` es un campo esperado en `location`. */
  validacion(ubicacion) {
    const comprobar = ubicacion ? `pm.expect(j.error.details.some((d) => d.location.includes(${JSON.stringify(ubicacion)}))).to.equal(true);` : '';
    return `errorContrato(422, 'REQUEST_VALIDATION_ERROR');
pm.test('422 describe qué campo falló, sin devolver el valor enviado (no hay "input")', () => {
  pm.expect(j.error.details).to.be.an('array').that.is.not.empty;
  j.error.details.forEach((d) => {
    pm.expect(Object.keys(d)).to.have.members(['location', 'message', 'type']);
  });
  ${comprobar}
});`;
  },
  /** Sin sesión válida: 401 con `INVALID_SESSION` y `WWW-Authenticate: Bearer`. */
  sinSesion() {
    return `errorContrato(401, 'INVALID_SESSION');
pm.test('401 anuncia el esquema Bearer', () => pm.expect(pm.response.headers.get('WWW-Authenticate')).to.equal('Bearer'));`;
  },
  prohibido() {
    return "errorContrato(403, 'FORBIDDEN');";
  },
  /** Guarda variables de la respuesta (solo si hay cuerpo JSON): `(j, set, v) => { set('x', j.id) }`. */
  guardar(fn, args = {}) {
    return `if (j) { ${invocar(fn, 'j, set, v, ' + JSON.stringify(args))} }`;
  },
  /** Guarda campos de la respuesta: `{ nombre_variable: 'j.campo' }` (las expresiones se evalúan en el sandbox). */
  guardarCampos(mapa) {
    const sentencias = Object.entries(mapa).map(([variable, expresion]) => `set(${JSON.stringify(variable)}, ${expresion});`);
    return `if (j) { ${sentencias.join(' ')} }`;
  },
  /** Cabecera de respuesta. */
  cabecera(nombre, esperado) {
    return `pm.test('cabecera ${nombre}', () => pm.expect(pm.response.headers.get(${JSON.stringify(nombre)})).to.equal(${JSON.stringify(esperado)}));`;
  },
};

/**
 * Una petición. Campos:
 *  nombre, descripcion, metodo, ruta (con {{variables}}), consulta {k: v}, token (variable con el JWT) o cabeceras,
 *  json (objeto) | jsonCrudo (texto con {{variables}} sin comillas) | formulario [{key, value} | {key, src}],
 *  previo (función `(v, set) => {...}` del sandbox), pruebas (array de código de T.*).
 */
export function peticion(def) {
  const {
    nombre, descripcion = '', metodo = 'GET', ruta, consulta, token, cabeceras = {}, json, jsonCrudo, formulario, previo, pruebas = [], ayudantes = [],
  } = def;
  if (!nombre || !ruta) throw new Error(`Petición sin nombre o ruta: ${JSON.stringify(def)}`);
  if (pruebas.length === 0) throw new Error(`La petición «${nombre}» no tiene aserciones.`);
  const header = [{ key: 'Accept', value: 'application/json' }];
  if (token) header.push({ key: 'Authorization', value: `Bearer {{${token}}}` });
  for (const [k, valor] of Object.entries(cabeceras)) header.push({ key: k, value: valor });
  const request = { method: metodo, header, url: `{{base_url}}${ruta}${consulta ? '?' + new URLSearchParams(consulta).toString().replace(/%7B%7B/g, '{{').replace(/%7D%7D/g, '}}') : ''}` };
  if (json !== undefined || jsonCrudo !== undefined) {
    header.push({ key: 'Content-Type', value: 'application/json' });
    request.body = { mode: 'raw', raw: jsonCrudo ?? JSON.stringify(json, null, 2), options: { raw: { language: 'json' } } };
  } else if (formulario) {
    request.body = {
      mode: 'formdata',
      formdata: formulario.map((campo) => (campo.src ? { key: campo.key, type: 'file', src: campo.src } : { key: campo.key, value: campo.value, type: 'text' })),
    };
  }
  if (descripcion) request.description = descripcion;
  const event = [];
  if (previo) event.push({ listen: 'prerequest', script: aScript([`const v = (n) => pm.variables.get(n); const set = (n, x) => { pm.collectionVariables.set(n, String(x)); if (!/token|pwd|password/i.test(n)) { pm.environment.set(n, String(x)); } };`, invocar(previo, 'v, set')]) });
  event.push({ listen: 'test', script: aScript([PRELUDIO, ...ayudantes, ...pruebas]) });
  return { name: nombre, event, request };
}

export function carpeta(nombre, descripcion, items) {
  return { name: nombre, description: descripcion, item: items };
}

export function coleccion({ nombre, descripcion, variables = {}, items }) {
  return {
    info: { name: nombre, description: descripcion, schema: VERSION_SCHEMA },
    item: items,
    variable: Object.entries(variables).map(([key, value]) => ({ key, value })),
  };
}

/** Cuenta las peticiones (hojas) de una colección o carpeta. */
export function contarPeticiones(nodo) {
  if (nodo.request) return 1;
  return (nodo.item ?? []).reduce((suma, hijo) => suma + contarPeticiones(hijo), 0);
}
