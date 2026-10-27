/**
 * Pruebas unitarias (sin Docker ni red) de la lógica del ejecutor de ZAP:
 *   node --test scripts/pruebas/unidad.test.mjs
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { clasificar, configuracionDelHook, plantillaARegex, validarAlcance } from '../lib/alcance.mjs';
import { SALIDA } from '../lib/codigos.mjs';
import { validarConfiguracion, validarDestinoQa } from '../lib/configuracion.mjs';
import { decidirSalida } from '../lib/decision.mjs';
import { especificacionFiltrada, IDENTIDAD_LOGIN_DE_ESCANEO, problemasDeDestino, validarEstructura } from '../lib/openapi.mjs';
import { MARCA, redactar, redactarJson, secretosRemanentes } from '../lib/redaccion.mjs';

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const alcance = validarAlcance(JSON.parse(readFileSync(join(RAIZ, 'alcance', 'operaciones.json'), 'utf8')));
const real = JSON.parse(readFileSync(join(RAIZ, 'pruebas', 'fixtures', 'openapi-real.json'), 'utf8'));
const BASE = 'http://igualab-backend-qa-backend-1:8000';
const EMPRESA = '11111111-2222-4333-8444-555555555555';

describe('alcance', () => {
  test('el OpenAPI real queda totalmente clasificado: 21 incluidas, 14 excluidas, ninguna sin decidir', () => {
    const c = clasificar(real, alcance);
    assert.equal(c.sinClasificar.length, 0);
    assert.equal(c.incluidas.length, 21);
    assert.equal(c.excluidas.length, 14);
    assert.equal(c.ausentes.length, 0);
  });

  test('las operaciones de OCI, DeepSeek, correo, credenciales, sesión y cuentas están excluidas', () => {
    const excluidas = new Set(clasificar(real, alcance).excluidas.map((o) => `${o.metodo} ${o.ruta}`));
    for (const esperada of [
      'POST /asistente/consultas', 'POST /documentos/operaciones/{operacion_id}/ingesta', 'POST /documentos/operaciones/{operacion_id}/reintentar-publicacion',
      'POST /auth/recuperar-contrasena', 'POST /auth/restablecer-contrasena', 'PATCH /auth/mi-contrasena', 'POST /auth/logout',
      'POST /usuarios', 'PATCH /usuarios/{usuario_id}/deshabilitar', 'PATCH /usuarios/{usuario_id}/habilitar', 'POST /usuarios/transferir-superadmin',
    ]) assert.ok(excluidas.has(esperada), `${esperada} debe estar excluida`);
  });

  test('una operación nueva queda «sin clasificar»', () => {
    const copia = structuredClone(real);
    copia.paths['/nuevo'] = { post: {} };
    assert.deepEqual(clasificar(copia, alcance).sinClasificar, [{ metodo: 'POST', ruta: '/nuevo' }]);
  });

  test('plantillaARegex distingue segmentos y no confunde rutas hermanas', () => {
    const r = new RegExp(plantillaARegex('/empresas/{empresa_id}'));
    assert.ok(r.test('/empresas/abc'));
    assert.ok(!r.test('/empresas/abc/desactivar'));
    assert.ok(!r.test('/empresas'));
    assert.ok(new RegExp(plantillaARegex('/usuarios/transferir-superadmin')).test('/usuarios/transferir-superadmin'));
  });

  test('barreras del hook: neutralización de métodos excluidos y retirada de credenciales fuera de las rutas de rol', () => {
    const { incluidas, excluidas } = clasificar(real, alcance);
    const config = configuracionDelHook({ baseUrl: BASE, incluidas, excluidas });
    const regla = (ruta) => config.neutralizar_metodos.find((r) => r.ruta === ruta);
    // Ruta compartida (GET incluido, POST excluido): solo se reescribe el método excluido.
    const usuarios = regla('/usuarios');
    assert.deepEqual(usuarios.metodos, ['POST']);
    assert.ok(new RegExp(usuarios.metodo_regex).test('POST http://x/usuarios HTTP/1.1') && !new RegExp(usuarios.metodo_regex).test('GET http://x/usuarios HTTP/1.1'));
    assert.ok(new RegExp(usuarios.url_regex).test(`${BASE}/usuarios`) && new RegExp(usuarios.url_regex).test(`${BASE}/usuarios?x=1`));
    assert.ok(!new RegExp(usuarios.url_regex).test(`${BASE}/usuarios/${EMPRESA}/habilitar`), 'no confunde rutas hermanas');
    assert.ok(!new RegExp(usuarios.url_regex).test('http://otro-host:8000/usuarios'));
    // Ruta con todos sus métodos excluidos: cualquier método.
    const logout = regla('/auth/logout');
    assert.deepEqual(logout.metodos, ['*']);
    assert.ok(new RegExp(logout.metodo_regex).test('PATCH http://x/auth/logout HTTP/1.1'));
    assert.equal(config.neutralizar_metodos.length, new Set(excluidas.map((o) => o.ruta)).size);
    // Retirada de credencial: no coincide con las rutas de rol, sí con todo lo demás (incluidos otros hosts).
    const sinCredencial = new RegExp(config.url_sin_credencial_regex);
    for (const url of [`${BASE}/usuarios`, `${BASE}/empresas/${EMPRESA}?a=b`, `${BASE}/reportes`, `${BASE}/analisis-gri/empresas/${EMPRESA}/anios/2026`, `${BASE}/auditoria`, `${BASE}/documentos`]) {
      assert.ok(!sinCredencial.test(url), `${url} debe conservar su credencial`);
    }
    for (const url of [`${BASE}/health`, `${BASE}/auth/login`, `${BASE}/latest/meta-data`, `${BASE}/.git/config`, 'http://otro-host:8000/usuarios', 'https://igualab-uat.ejemplo.pe/usuarios', `${BASE}/usuarios-x`]) {
      assert.ok(sinCredencial.test(url), `${url} NO debe llevar credencial`);
    }
  });

  test('configuración del hook: un token por rol con filtro de URL por prefijo y exclusiones solo de rutas totalmente vetadas', () => {
    const { incluidas, excluidas } = clasificar(real, alcance);
    const config = configuracionDelHook({ baseUrl: BASE, incluidas, excluidas });
    assert.deepEqual(Object.keys(config.roles).sort(), ['administrador', 'superadmin']);
    const superadmin = new RegExp(config.roles.superadmin.url_regex);
    const administrador = new RegExp(config.roles.administrador.url_regex);
    assert.ok(superadmin.test(`${BASE}/usuarios`) && superadmin.test(`${BASE}/empresas/${EMPRESA}?x=1`) && superadmin.test(`${BASE}/auditoria`));
    assert.ok(administrador.test(`${BASE}/reportes`) && administrador.test(`${BASE}/analisis-gri/empresas/${EMPRESA}/anios/2026`));
    assert.ok(!superadmin.test(`${BASE}/reportes`) && !administrador.test(`${BASE}/usuarios`));
    assert.ok(!superadmin.test('http://otro-host:8000/usuarios'), 'el token solo se inyecta hacia el destino');
    assert.ok(!superadmin.test(`${BASE}/health`) && !superadmin.test(`${BASE}/auth/login`), 'login y health se piden sin credenciales');
    const vetadas = config.excluir_del_ataque.map((r) => new RegExp(r));
    assert.ok(vetadas.some((r) => r.test(`${BASE}/asistente/consultas`)));
    assert.ok(vetadas.some((r) => r.test(`${BASE}/auth/restablecer-contrasena`)));
    assert.ok(!vetadas.some((r) => r.test(`${BASE}/usuarios`)), '/usuarios tiene un GET incluido: no puede vetarse por ruta');
    assert.ok(!vetadas.some((r) => r.test(`${BASE}/empresas/${EMPRESA}`)));
    assert.ok(!vetadas.some((r) => r.test(`${BASE}/documentos/operaciones/00000000-0000-4000-8000-000000000000`)), 'el GET de operación sigue incluido');
    assert.ok(vetadas.some((r) => r.test(`${BASE}/documentos/operaciones`)));
  });
});

describe('OpenAPI: destinos y especificación filtrada', () => {
  test('el OpenAPI real no tiene destinos de otros ambientes', () => {
    assert.deepEqual(problemasDeDestino(real, BASE), []);
    validarEstructura(real);
  });

  test('se rechazan servidores ajenos, referencias remotas y URLs de seguridad externas', () => {
    const a = structuredClone(real); a.servers = [{ url: 'https://igualab-uat.ejemplo.pe' }];
    assert.equal(problemasDeDestino(a, BASE).length, 1);
    const b = structuredClone(real); b.paths['/auth/login'].post.servers = [{ url: 'http://otro:8000' }];
    assert.equal(problemasDeDestino(b, BASE).length, 1);
    const c = structuredClone(real); c.components.schemas.X = { $ref: 'http://ajeno/e.json' };
    assert.match(problemasDeDestino(c, BASE)[0], /\$ref no local/);
    const d = structuredClone(real); d.components.securitySchemes = { o: { type: 'oauth2', flows: { password: { tokenUrl: 'https://login.ajeno.com/t' } } } };
    assert.match(problemasDeDestino(d, BASE)[0], /tokenUrl/);
    const permitido = structuredClone(real); permitido.servers = [{ url: BASE }, { url: '/' }];
    assert.deepEqual(problemasDeDestino(permitido, BASE), []);
  });

  test('estructura inválida: no es OpenAPI 3 o no tiene rutas', () => {
    assert.throws(() => validarEstructura({ swagger: '2.0', paths: { '/': {} } }), /OpenAPI 3/);
    assert.throws(() => validarEstructura({ openapi: '3.1.0', paths: {} }), /paths vacío/);
    assert.throws(() => validarEstructura([]), /objeto JSON/);
  });

  test('la especificación filtrada solo contiene operaciones incluidas, apunta al destino y no altera la original', () => {
    const original = structuredClone(real);
    const { incluidas } = clasificar(real, alcance);
    const filtrada = especificacionFiltrada(real, { baseUrl: BASE, incluidas, empresaId: EMPRESA, anio: 2026 });
    assert.deepEqual(real, original, 'el OpenAPI publicado por la aplicación no se modifica');
    const en = new Set();
    for (const [ruta, item] of Object.entries(filtrada.paths)) for (const m of ['get', 'post', 'put', 'patch', 'delete']) if (item[m]) en.add(`${m.toUpperCase()} ${ruta}`);
    assert.equal(en.size, 21);
    for (const o of incluidas) assert.ok(en.has(`${o.metodo} ${o.ruta}`));
    assert.ok(!en.has('POST /asistente/consultas') && !en.has('POST /auth/logout') && !en.has('POST /usuarios'));
    assert.ok(filtrada.paths['/usuarios'].get && !filtrada.paths['/usuarios'].post, 'GET /usuarios queda y POST /usuarios no');
    assert.ok(filtrada.paths['/empresas'].get && !filtrada.paths['/empresas'].post);
    assert.ok(!('/asistente/consultas' in filtrada.paths) && !('/auth/logout' in filtrada.paths));
    assert.deepEqual(filtrada.servers, [{ url: 'http://igualab-backend-qa-backend-1:8000' }]);
    assert.deepEqual(problemasDeDestino(filtrada, BASE), []);
  });

  test('los ejemplos dirigen las peticiones al recurso sintético y el login usa una identidad inexistente', () => {
    const { incluidas } = clasificar(real, alcance);
    const f = especificacionFiltrada(real, { baseUrl: BASE, incluidas, empresaId: EMPRESA, anio: 2026 });
    const parametros = f.paths['/empresas/{empresa_id}'].get.parameters;
    assert.equal(parametros.find((p) => p.name === 'empresa_id').example, EMPRESA);
    assert.equal(f.components.schemas.LoginRequest.properties.correo.example, IDENTIDAD_LOGIN_DE_ESCANEO.correo);
    assert.match(IDENTIDAD_LOGIN_DE_ESCANEO.correo, /zap-escaner-sin-cuenta@/);
    const sectores = f.components.schemas.GenerarReporteRequest?.properties?.empresa_id;
    if (sectores) assert.equal(sectores.example, EMPRESA);
  });

  test('las consultas de disponibilidad reciben la empresa sintética y el año válido, y se escanean con el rol de su prefijo', () => {
    const { incluidas } = clasificar(real, alcance);
    const f = especificacionFiltrada(real, { baseUrl: BASE, incluidas, empresaId: EMPRESA, anio: 2026 });
    for (const ruta of ['/documentos/disponibilidad', '/reportes/disponibilidad']) {
      const operacion = f.paths[ruta]?.get;
      assert.ok(operacion, `${ruta} debe estar en la especificación filtrada`);
      const porNombre = Object.fromEntries(operacion.parameters.map((p) => [p.name, p]));
      assert.equal(porNombre.empresa_id.example, EMPRESA, `${ruta}: empresa sintética`);
      assert.equal(porNombre.empresa_id.in, 'query');
      assert.equal(porNombre.anio.example, '2026', `${ruta}: año de la ejecución`);
      assert.equal(porNombre.anio.in, 'query');
      assert.equal(porNombre.empresa_id.required, true);
    }
    assert.equal(f.paths['/documentos/disponibilidad'].get.parameters.find((p) => p.name === 'anio').required, true);
    assert.notEqual(f.paths['/reportes/disponibilidad'].get.parameters.find((p) => p.name === 'anio').required, true, 'en reportes el año es opcional');
    const rolDe = (ruta) => incluidas.find((o) => o.metodo === 'GET' && o.ruta === ruta).rol;
    assert.equal(rolDe('/documentos/disponibilidad'), 'superadmin');
    assert.equal(rolDe('/reportes/disponibilidad'), 'administrador');
    // El hook inyecta la credencial por prefijo: /documentos → SuperAdmin; /reportes → Administrador sintético.
    const config = configuracionDelHook({ baseUrl: BASE, incluidas, excluidas: clasificar(real, alcance).excluidas });
    assert.ok(new RegExp(config.roles.superadmin.url_regex).test(`${BASE}/documentos/disponibilidad?empresa_id=${EMPRESA}&anio=2026`));
    assert.ok(new RegExp(config.roles.administrador.url_regex).test(`${BASE}/reportes/disponibilidad?empresa_id=${EMPRESA}`));
    assert.ok(!new RegExp(config.roles.administrador.url_regex).test(`${BASE}/documentos/disponibilidad?empresa_id=${EMPRESA}`));
  });
});

describe('redacción de secretos', () => {
  const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c3VhcmlvIn0.firmaDeEjemplo12345';

  test('JWT, Bearer y Authorization se sustituyen aunque no se conozca el valor', () => {
    const { texto, sustituciones } = redactar(`GET /x\nAuthorization: Bearer ${jwt}\n{"access_token":"${jwt}"} Bearer abcdefghijklmnopqrstuvwxyz`);
    assert.ok(!texto.includes(jwt) && !/eyJ/.test(texto));
    assert.match(texto, /Authorization: \[REDACTADO\]/);
    assert.ok(sustituciones >= 3);
    assert.deepEqual(secretosRemanentes(texto), []);
  });

  test('JSON: la cabecera con \\r\\n escapados se redacta sin romper el JSON', () => {
    const json = JSON.stringify({ requestHeader: `GET / HTTP/1.1\r\nAuthorization: Bearer ${jwt}\r\nHost: x\r\n`, evidence: "' OR 1=1--" });
    const { texto } = redactar(json);
    const analizado = JSON.parse(texto);
    assert.match(analizado.requestHeader, /Authorization: \[REDACTADO\]\r\nHost: x/);
    assert.equal(analizado.evidence, "' OR 1=1--");
  });

  test('el nombre de la cabecera como evidencia de ZAP ("authorization:") no se consume ni rompe el JSON (caso real)', () => {
    const json = JSON.stringify({ site: [{ alerts: [{ instances: [{ evidence: 'authorization:', otherinfo: '' }] }] }] }, null, '\t');
    const { texto } = redactar(json);
    assert.equal(texto, json, 'sin credencial no hay nada que sustituir');
    for (const evidencia of ['authorization:', 'Authorization:', 'authorization: ']) {
      const entrada = JSON.stringify({ e: evidencia, otro: 'x' });
      assert.equal(JSON.parse(redactar(entrada).texto).otro, 'x');
    }
  });

  test('redactarJson recorre las cadenas: siempre produce JSON válido y conserva la estructura', () => {
    const doc = { a: [{ evidence: `Authorization: Bearer ${jwt}`, n: 3, ok: true }], b: null, clave: 'Aa1!xyz9' };
    const { texto, sustituciones, estructural } = redactarJson(JSON.stringify(doc, null, '\t'), ['Aa1!xyz9']);
    const salida = JSON.parse(texto);
    assert.ok(estructural && sustituciones >= 2);
    assert.equal(salida.a[0].n, 3);
    assert.equal(salida.b, null);
    assert.equal(salida.clave, MARCA);
    assert.ok(!texto.includes(jwt));
    assert.equal(redactarJson('no es json {', []).estructural, false);
  });

  test('valores secretos conocidos, también escapados para JSON, HTML y URL', () => {
    const secreto = 'Aa1!x"<y>&z\'q';
    const variantes = [secreto, JSON.stringify(secreto).slice(1, -1), encodeURIComponent(secreto), "Aa1!x&quot;&lt;y&gt;&amp;z&#x27;q", 'Aa1!x&quot;&lt;y&gt;&amp;z&#39;q'];
    for (const v of variantes) {
      const { texto } = redactar(`dato ${v} fin`, [secreto]);
      assert.ok(!texto.includes(v), `no se redactó la variante ${v}`);
      assert.deepEqual(secretosRemanentes(texto, [secreto]), []);
    }
    assert.ok(secretosRemanentes(`algo ${secreto}`, [secreto]).length > 0, 'la verificación detecta un secreto que quedó');
  });

  test('se conserva la evidencia que no es secreta', () => {
    const entrada = 'Parámetro q con ataque \' OR 1=1-- en http://host:8000/empresas (HTTP 500)';
    assert.equal(redactar(entrada, ['otro-secreto-largo']).texto, entrada);
    assert.equal(MARCA, '[REDACTADO]');
  });
});

describe('decisión del veredicto', () => {
  const hookBueno = () => ({
    violaciones_total: 0, violaciones_de_alcance: [], urls_fuera_del_destino: [],
    analisis_activo: { ejecutado: true, completo: true }, cobertura_autenticada: { demostrada: true },
  });
  const base = () => ({ prepCodigo: 0, zapEjecutado: true, zapCodigo: 0, tiempoAgotado: false, reportes: { ok: true }, secretos: { ok: true }, hook: hookBueno(), limpieza: { ok: true } });
  const codigo = (cambios) => decidirSalida({ ...base(), ...cambios }).codigo;

  test('0/1/2 del script oficial se conservan', () => {
    assert.equal(codigo({}), SALIDA.OK);
    assert.equal(codigo({ zapCodigo: 2 }), SALIDA.ADVERTENCIAS);
    assert.equal(codigo({ zapCodigo: 1 }), SALIDA.REGLAS_FAIL);
  });

  test('cualquier otro código de ZAP es un error técnico', () => {
    for (const z of [3, 4, 124, 137, null]) assert.equal(codigo({ zapCodigo: z }), SALIDA.ERROR_ZAP, `código ${z}`);
    assert.equal(codigo({ zapEjecutado: false, zapCodigo: null }), SALIDA.ERROR_ZAP);
  });

  test('sin evidencia no hay éxito: reportes, hook, análisis activo y cobertura autenticada', () => {
    assert.equal(codigo({ reportes: { ok: false } }), SALIDA.REPORTES);
    assert.equal(codigo({ hook: null }), SALIDA.EVIDENCIA);
    assert.equal(codigo({ hook: { ...hookBueno(), analisis_activo: { ejecutado: false } } }), SALIDA.EVIDENCIA);
    assert.equal(codigo({ hook: { ...hookBueno(), cobertura_autenticada: { demostrada: false } } }), SALIDA.EVIDENCIA);
    assert.equal(codigo({ secretos: { ok: false } }), SALIDA.SECRETOS);
    assert.equal(codigo({ limpieza: { ok: false } }), SALIDA.LIMPIEZA);
  });

  test('un FAIL no oculta un problema técnico, y la violación de alcance tiene prioridad sobre el código de ZAP', () => {
    assert.equal(codigo({ zapCodigo: 1, reportes: { ok: false } }), SALIDA.REPORTES);
    assert.equal(codigo({ zapCodigo: 1, hook: null }), SALIDA.EVIDENCIA);
    assert.equal(codigo({ zapCodigo: 3, hook: { ...hookBueno(), abortado_antes_del_ataque: true } }), SALIDA.ALCANCE);
    assert.equal(codigo({ zapCodigo: 0, hook: { ...hookBueno(), violaciones_total: 2 } }), SALIDA.ALCANCE);
    assert.equal(codigo({ zapCodigo: 0, hook: { ...hookBueno(), autorizacion_incorrecta_total: 1 } }), SALIDA.ALCANCE, 'token de otro rol, host ajeno o ruta pública');
  });

  test('análisis activo parcial: nunca éxito', () => {
    assert.equal(codigo({ hook: { ...hookBueno(), analisis_activo: { ejecutado: true, completo: false } } }), SALIDA.ADVERTENCIAS);
  });

  test('timeout, interrupción y fallos de preparación', () => {
    assert.equal(codigo({ tiempoAgotado: true, zapCodigo: 124 }), SALIDA.TIEMPO_AGOTADO);
    assert.equal(codigo({ interrumpido: true, zapEjecutado: false, zapCodigo: null }), SALIDA.TIEMPO_AGOTADO);
    assert.equal(codigo({ prepCodigo: SALIDA.CONFIGURACION, zapEjecutado: false, zapCodigo: null }), SALIDA.CONFIGURACION);
    assert.equal(codigo({ prepCodigo: SALIDA.BACKEND_NO_DISPONIBLE, zapEjecutado: false, zapCodigo: null }), SALIDA.BACKEND_NO_DISPONIBLE);
    assert.equal(codigo({ prepCodigo: 1, zapEjecutado: false, zapCodigo: null }), SALIDA.ERROR_ZAP, 'una preparación con código 1 no se confunde con «reglas FAIL»');
  });
});

describe('configuración y destino QA', () => {
  const entorno = (extra = {}) => ({
    IGUALAB_API_URL: BASE, IGUALAB_PRUEBAS_ENTORNO: 'qa', IGUALAB_E2E_SUPERADMIN_CORREO: 'oscar@x.pe', IGUALAB_E2E_SUPERADMIN_PASSWORD: 'secreto-no-mostrar', ...extra,
  });

  test('configuración válida para el contenedor de QA', () => {
    const c = validarConfiguracion(entorno());
    assert.equal(c.baseUrl, BASE);
    assert.equal(c.destino.porNombre, true);
  });

  test('QA por nombre; UAT, desarrollo y producción se rechazan; un host sin marca exige confirmación', () => {
    const url = (h) => new URL(`http://${h}:8000`);
    assert.ok(validarDestinoQa(url('qa.igualab.pe'), { IGUALAB_PRUEBAS_ENTORNO: 'qa' }).porNombre);
    for (const h of ['igualab-backend-uat-backend-1', 'igualab-backend-development-backend-1', 'api.prod.igualab.pe', 'staging.igualab.pe']) {
      assert.throws(() => validarDestinoQa(url(h), { IGUALAB_PRUEBAS_ENTORNO: 'qa', ZAP_CONFIRMO_DESTINO_QA: h }), /otro ambiente/, h);
    }
    assert.throws(() => validarDestinoQa(url('10.0.0.5'), { IGUALAB_PRUEBAS_ENTORNO: 'qa' }), /ZAP_CONFIRMO_DESTINO_QA/);
    assert.ok(validarDestinoQa(url('10.0.0.5'), { IGUALAB_PRUEBAS_ENTORNO: 'qa', ZAP_CONFIRMO_DESTINO_QA: '10.0.0.5' }).porConfirmacion);
    assert.throws(() => validarDestinoQa(url('qa.igualab.pe'), { IGUALAB_PRUEBAS_ENTORNO: 'uat' }), /exactamente "qa"/);
  });

  test('en Jenkins solo se acepta la rama qa', () => {
    assert.throws(() => validarConfiguracion(entorno({ JENKINS_URL: 'http://j', BRANCH_NAME: 'uat' })), /rama qa/);
    assert.throws(() => validarConfiguracion(entorno({ BUILD_NUMBER: '3' })), /rama qa/);
    assert.ok(validarConfiguracion(entorno({ JENKINS_URL: 'http://j', BRANCH_NAME: 'qa' })));
  });

  test('la URL no admite ruta, credenciales ni consulta, y los faltantes se informan por nombre', () => {
    for (const u of [`${BASE}/api`, 'http://u:p@igualab-qa:8000', `${BASE}?x=1`, 'no-es-url']) {
      assert.throws(() => validarConfiguracion(entorno({ IGUALAB_API_URL: u })), /IGUALAB_API_URL/);
    }
    try { validarConfiguracion(entorno({ IGUALAB_E2E_SUPERADMIN_PASSWORD: '' })); assert.fail('debía fallar'); } catch (e) {
      assert.match(e.message, /IGUALAB_E2E_SUPERADMIN_PASSWORD/);
      assert.ok(!e.message.includes('secreto-no-mostrar'));
    }
  });
});

describe('archivo de reglas FAIL/WARN/IGNORE', () => {
  const texto = readFileSync(join(RAIZ, 'reglas', 'zap-api-scan.reglas.conf'), 'utf8');
  const lineas = texto.split('\n').filter((l) => l && !l.startsWith('#'));

  test('cumple el formato que exige zap-api-scan.py (id, nivel válido y mensaje, separados por tabuladores) y es ASCII', () => {
    assert.ok(/^[\x00-\x7f]*$/.test(texto), 'ASCII puro: la lectura no depende de la configuración regional del contenedor');
    const ids = new Set();
    for (const l of lineas) {
      assert.ok((l.match(/\t/g) ?? []).length >= 2, l);
      const [id, nivel] = l.split('\t');
      assert.ok(['PASS', 'IGNORE', 'INFO', 'WARN', 'FAIL'].includes(nivel), l);
      assert.ok(!ids.has(id), `regla duplicada ${id}`);
      ids.add(id);
    }
  });

  test('cada IGNORE está justificado en el propio archivo: tres por OAST externo y 40025 por indicación expresa del asesor (Caddy)', () => {
    const ignorados = lineas.filter((l) => l.split('\t')[1] === 'IGNORE').map((l) => l.split('\t')[0]);
    assert.deepEqual(ignorados.sort(), ['40025', '40043', '40046', '40047']);
    for (const id of ignorados) assert.match(texto, new RegExp(`#.*${id}`), `falta la justificación de ${id}`);
    assert.match(texto, /OAST/);
    assert.match(lineas.find((l) => l.startsWith('40025\t')), /asesor del curso/);
    assert.match(texto, /#.*Caddy/);
    assert.match(texto, /UNICA excepcion/i, '40025 se documenta como excepción única');
  });

  test('las reglas IGNORE se describen como cobertura NO evaluada, nunca como vulnerabilidad descartada', () => {
    const ignoradas = lineas.filter((l) => l.split('\t')[1] === 'IGNORE');
    assert.equal(ignoradas.length, 4);
    for (const l of ignoradas) assert.match(l, /NO evaluada/, l);
    for (const l of ignoradas) assert.ok(!/descartad[ao]/i.test(l.replace(/no es un descarte/gi, '')), l);
  });

  test('las inyecciones y la ejecución de código son FAIL', () => {
    const nivel = (id) => lineas.find((l) => l.startsWith(`${id}\t`))?.split('\t')[1];
    for (const id of ['40018', '40012', '6', '90020', '90019', '90023', '90035']) assert.equal(nivel(id), 'FAIL', id);
  });
});
