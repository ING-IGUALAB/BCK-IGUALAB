/**
 * Protección de secretos en lo que se archiva. Conserva la evidencia útil de las vulnerabilidades y sustituye solo:
 *   - JWT (aunque no se conozca su valor), cabeceras `Authorization: …` y `Bearer …`;
 *   - los valores secretos CONOCIDOS de esta ejecución (contraseñas y tokens), también escapados para JSON, HTML y URL.
 */
export const MARCA = '[REDACTADO]';

const JWT = /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*/g;
const BEARER = /\b(Bearer)(\s|&#32;|&nbsp;|%20)+[A-Za-z0-9._~+/=-]{12,}/gi;
// La cabecera solo se redacta si lo que sigue PARECE una credencial (esquema opcional + 8 o más caracteres de token, o una marca ya puesta).
// Sin \b: en JSON la cabecera llega como `\r\nAuthorization: …` y la `n` del escape no deja un límite de palabra. ZAP usa a veces el
// texto `"authorization:"` como evidencia (solo el nombre de la cabecera): no debe consumirse nada de lo que le sigue.
const CABECERA_AUTORIZACION = /(Authorization["']?\s*[:=]\s*["']?)(?:(?:Bearer|Basic|Digest|Token)\s+)?(?:[A-Za-z0-9._~+/=-]{8,}|\[REDACTADO\])/gi;

const escaparHtml = (texto, comilla) =>
  texto.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, comilla);

function variantes(secreto) {
  const lista = new Set([secreto]);
  lista.add(JSON.stringify(secreto).slice(1, -1));
  lista.add(encodeURIComponent(secreto));
  lista.add(escaparHtml(secreto, '&#x27;'));
  lista.add(escaparHtml(secreto, '&#39;'));
  return [...lista].filter((v) => v.length >= 6);
}

/** Devuelve { texto, sustituciones } sin los patrones ni los secretos conocidos. */
export function redactar(texto, secretos = []) {
  let sustituciones = 0;
  const contar = (regex, reemplazo) => {
    texto = texto.replace(regex, (...args) => {
      sustituciones += 1;
      return typeof reemplazo === 'function' ? reemplazo(...args) : reemplazo;
    });
  };
  contar(JWT, MARCA);
  contar(CABECERA_AUTORIZACION, (_todo, prefijo) => `${prefijo}${MARCA}`);
  contar(BEARER, (_todo, palabra) => `${palabra} ${MARCA}`);
  const conocidos = [...new Set(secretos.filter((s) => typeof s === 'string' && s.length >= 6).flatMap(variantes))].sort((a, b) => b.length - a.length);
  for (const valor of conocidos) {
    if (!texto.includes(valor)) continue;
    const partes = texto.split(valor);
    sustituciones += partes.length - 1;
    texto = partes.join(MARCA);
  }
  return { texto, sustituciones };
}

/**
 * Redacción de un documento JSON: recorre las CADENAS y redacta cada una, de modo que el resultado es siempre JSON válido (una
 * sustitución por expresiones regulares sobre el texto crudo podría romper la sintaxis). Si no se puede interpretar como JSON se
 * redacta como texto. Conserva la sangría con tabuladores que usa ZAP.
 */
export function redactarJson(texto, secretos = []) {
  let documento;
  try {
    documento = JSON.parse(texto);
  } catch {
    return { ...redactar(texto, secretos), estructural: false };
  }
  let sustituciones = 0;
  const recorrer = (nodo) => {
    if (typeof nodo === 'string') {
      const r = redactar(nodo, secretos);
      sustituciones += r.sustituciones;
      return r.texto;
    }
    if (Array.isArray(nodo)) return nodo.map(recorrer);
    if (nodo && typeof nodo === 'object') return Object.fromEntries(Object.entries(nodo).map(([k, v]) => [k, recorrer(v)]));
    return nodo;
  };
  return { texto: `${JSON.stringify(recorrer(documento), null, '\t')}\n`, sustituciones, estructural: true };
}

/** Lista de hallazgos que aún quedan (vacía = limpio). Se usa como verificación posterior a redactar. */
export function secretosRemanentes(texto, secretos = []) {
  const hallazgos = [];
  if (new RegExp(JWT.source).test(texto)) hallazgos.push('forma de JWT');
  if (/\bBearer\s+[A-Za-z0-9._~+/=-]{20,}/i.test(texto)) hallazgos.push('cabecera Bearer');
  if (/Authorization["']?\s*[:=]\s*["']?(?!\[REDACTADO\])[A-Za-z0-9._~+/=-]{12,}/i.test(texto)) hallazgos.push('cabecera Authorization');
  for (const secreto of secretos.filter((s) => typeof s === 'string' && s.length >= 6)) {
    if (variantes(secreto).some((v) => texto.includes(v))) hallazgos.push('valor secreto conocido');
  }
  return [...new Set(hallazgos)];
}
