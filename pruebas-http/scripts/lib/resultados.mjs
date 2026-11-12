/**
 * Registro de la ejecución y reportes. Todo lo que se escribe aquí sale de campos permitidos explícitamente (nombre de la
 * petición, método, RUTA sin consulta, estado HTTP, tiempo y resultado de las aserciones): NUNCA cabeceras, cuerpos, tokens ni
 * contraseñas. Además `buscarSecretos` revisa los archivos escritos antes de darlos por buenos.
 */
import { appendFileSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

/** Prefijo de la prueba con que la guarda de dependencias de las colecciones registra una petición NO enviada (el mismo texto
 *  que `PREFIJO_BLOQUEADA` de colecciones/fuentes/dsl.mjs, que no se copia a la imagen: la colección viaja ya generada). */
export const PREFIJO_BLOQUEADA = 'BLOQUEADA';

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

// ---- Diagnóstico de una respuesta inesperada -----------------------------------------------------------------------------
// Cuando una petición ENVIADA falla alguna aserción se conserva, del cuerpo de error del contrato del backend
// (`{"error": {code, message, details, request_id}}`), solo lo necesario para localizar la causa en los logs del servidor.
// Nunca se copia el cuerpo tal cual: si no es ese contrato solo se anota su tipo y tamaño.
const LIMITE_CUERPO = 65_536; // bytes que se intentan interpretar; un cuerpo mayor no se analiza
const LIMITE_MENSAJE = 300;
const LIMITE_TEXTO = 160;
const LIMITE_DETALLES = 1_200; // caracteres del JSON de `details` ya saneado
const CLAVE_SENSIBLE = /token|password|contrasen|secret|authorization|cookie|credential|api[_-]?key|clave|pwd|contenido|texto|fragmento/i;
const FORMA_SECRETA = /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|Bearer\s+\S{12,}/;
const CODIGO_VALIDO = /^[A-Za-z0-9_.:-]{1,80}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const OMITIDO = '[omitido]';

function textoSeguro(valor, limite) {
  const texto = String(valor).replace(/[\u0000-\u001f\u007f]+/g, ' ');
  if (FORMA_SECRETA.test(texto)) return OMITIDO;
  return texto.length > limite ? `${texto.slice(0, limite)}…(+${texto.length - limite})` : texto;
}

function sanear(valor, nivel = 0) {
  if (valor === null || typeof valor === 'number' || typeof valor === 'boolean') return valor;
  if (typeof valor === 'string') return textoSeguro(valor, LIMITE_TEXTO);
  if (typeof valor !== 'object' || nivel >= 4) return OMITIDO;
  if (Array.isArray(valor)) {
    const lista = valor.slice(0, 10).map((v) => sanear(v, nivel + 1));
    if (valor.length > 10) lista.push(`…(+${valor.length - 10})`);
    return lista;
  }
  const salida = {};
  for (const [clave, v] of Object.entries(valor).slice(0, 20)) {
    salida[textoSeguro(clave, 60)] = CLAVE_SENSIBLE.test(clave) ? OMITIDO : sanear(v, nivel + 1);
  }
  return salida;
}

/**
 * Resumen SANEADO y de tamaño limitado de una respuesta que no era la esperada. `cuerpo` es el Buffer de la respuesta y
 * `cabeceras` una función `nombre → valor`. Devuelve solo: tipo y tamaño del cuerpo, `request_id` (cabecera X-Request-ID o el
 * del cuerpo, si es un UUID), y —si el cuerpo es el contrato de errores— `codigo`, `mensaje` y `detalles` saneados.
 */
export function diagnosticoDeRespuesta({ cuerpo, cabeceras = () => undefined }) {
  const bytes = cuerpo?.length ?? 0;
  const tipo = textoSeguro(String(cabeceras('content-type') ?? '').split(';')[0].trim() || 'desconocido', 60);
  const salida = { tipo_contenido: tipo, bytes_cuerpo: bytes };
  const deCabecera = String(cabeceras('x-request-id') ?? '');
  if (UUID.test(deCabecera)) salida.request_id = deCabecera;
  if (!bytes || bytes > LIMITE_CUERPO || !/json/i.test(tipo)) return salida;
  let error;
  try {
    error = JSON.parse(Buffer.from(cuerpo).toString('utf8'))?.error;
  } catch {
    return { ...salida, cuerpo_no_interpretable: true };
  }
  if (!error || typeof error !== 'object' || Array.isArray(error)) return { ...salida, sin_contrato_de_error: true };
  if (typeof error.code === 'string') salida.codigo = CODIGO_VALIDO.test(error.code) ? error.code : OMITIDO;
  if (typeof error.message === 'string') salida.mensaje = textoSeguro(error.message, LIMITE_MENSAJE);
  if (!salida.request_id && typeof error.request_id === 'string' && UUID.test(error.request_id)) salida.request_id = error.request_id;
  if (error.details !== undefined && error.details !== null) {
    const detalles = sanear(error.details);
    const serializado = JSON.stringify(detalles);
    salida.detalles = serializado.length <= LIMITE_DETALLES
      ? detalles
      : { truncado: true, caracteres: serializado.length, inicio: serializado.slice(0, LIMITE_DETALLES) };
  }
  return salida;
}

/** Una línea legible del diagnóstico para la consola y el resumen. */
export function lineaDeDiagnostico(d) {
  const partes = [`request_id=${d.request_id ?? 'no disponible'}`];
  if (d.codigo) partes.push(`código=${d.codigo}`);
  if (d.mensaje) partes.push(`mensaje=${d.mensaje}`);
  if (d.detalles !== undefined) partes.push(`detalles=${JSON.stringify(d.detalles)}`);
  if (!d.codigo) partes.push(`cuerpo ${d.tipo_contenido} de ${d.bytes_cuerpo} bytes sin el contrato de errores (no se copia)`);
  return partes.join(' · ');
}

export function escribirResumen({ directorio, contexto, peticiones, estadisticas, fallos, datos, veredicto }) {
  const resumen = { ...contexto, veredicto, estadisticas, fallos, peticiones, datos_sinteticos_creados: datos };
  writeFileSync(join(directorio, 'resumen.json'), `${JSON.stringify(resumen, null, 2)}\n`);
  const filas = peticiones.map((p) => {
    const marca = p.bloqueada ? 'BLOQUEADA' : p.aserciones_fallidas === 0 && p.estado_http !== null ? 'OK' : 'FALLA';
    return `| ${marca} | ${p.carpeta} | ${p.nombre.replaceAll('|', '/')} | ${p.metodo} | ${p.ruta} | ${p.estado_http ?? '—'} | ${p.milisegundos} | ${p.aserciones_total - p.aserciones_fallidas}/${p.aserciones_total} |`;
  });
  const lineas = [
    `# Resultado de ${contexto.coleccion} (${contexto.entorno})`,
    '',
    `- Veredicto: **${veredicto}**`,
    `- Identificador de ejecución: \`${contexto.run_id}\``,
    `- Backend: \`${contexto.host}\``,
    `- Peticiones: ${estadisticas.peticiones_ejecutadas}/${estadisticas.peticiones_esperadas}${estadisticas.peticiones_bloqueadas ? ` (bloqueadas, no enviadas: ${estadisticas.peticiones_bloqueadas})` : ''} · aserciones: ${estadisticas.aserciones_total} (fallidas: ${estadisticas.aserciones_fallidas})`,
    `- Duración: ${(estadisticas.duracion_ms / 1000).toFixed(1)} s`,
    '',
    ...(fallos.length ? ['## Fallos', '', ...fallos.map((f) => `- **${f.peticion}** — ${f.prueba}: ${f.mensaje}`), ''] : []),
    ...(peticiones.some((p) => p.respuesta_inesperada)
      ? [
        '## Respuestas inesperadas (diagnóstico saneado)',
        '',
        ...peticiones.filter((p) => p.respuesta_inesperada).map((p) => `- **${p.nombre.replaceAll('|', '/')}** — HTTP ${p.estado_http}, enviada ${p.enviada_utc ?? '—'} (UTC): ${lineaDeDiagnostico(p.respuesta_inesperada).replaceAll('|', '/')}`),
        '',
      ]
      : []),
    '## Peticiones',
    '',
    '| Resultado | Carpeta | Petición | Método | Ruta | HTTP | ms | Aserciones |',
    '|---|---|---|---|---|---|---|---|',
    ...filas,
    '',
    '> Este informe no contiene cabeceras, cuerpos, tokens ni contraseñas. De una respuesta inesperada solo se conservan el código, el mensaje, el request_id y los detalles saneados del error.',
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
