/**
 * Verificación LOCAL Y AISLADA del ejecutor (ejecutar-zap.sh + preparar.mjs + finalizar.mjs) con un `docker` falso y un backend falso.
 * Comprueba la lógica de orquestación: códigos de salida (éxito, advertencia, FAIL, error técnico), límite global y eliminación SOLO
 * del contenedor de ZAP de la ejecución, cierre de datos sintéticos, protección de secretos y rechazo de destinos no permitidos.
 *
 * NO es un escaneo: ningún ZAP real corre aquí y el backend no es el desplegado. El comportamiento real de ZAP solo lo demuestra Jenkins.
 *   node --test scripts/pruebas/ejecutor.test.mjs
 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { after, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { crearBackendFalso } from '../../pruebas/falsos/backend-falso.mjs';

const aqui = dirname(fileURLToPath(import.meta.url));
const RAIZ = resolve(aqui, '..', '..');
const FALSOS = join(RAIZ, 'pruebas', 'falsos');
const SCRIPT = join(RAIZ, 'scripts', 'ejecutar-zap.sh');
const temporales = [];
let secuencia = 0;

after(() => { for (const t of temporales) rmSync(t, { recursive: true, force: true }); });

function leerTodo(directorio) {
  if (!existsSync(directorio)) return '';
  return readdirSync(directorio, { recursive: true })
    .map((f) => join(directorio, f))
    .filter((f) => statSync(f).isFile())
    .map((f) => readFileSync(f, 'utf8'))
    .join('\n');
}

function ejecutar(args, entorno, { alIniciar } = {}) {
  return new Promise((resolver) => {
    const hijo = spawn(process.env.SH_BIN ?? 'sh', [SCRIPT, ...args], { env: entorno, stdio: ['ignore', 'pipe', 'pipe'] });
    let salida = '';
    let terminado = false;
    const fin = (codigo) => { if (!terminado) { terminado = true; resolver({ codigo, salida }); } };
    hijo.stdout.on('data', (d) => { salida += d; });
    hijo.stderr.on('data', (d) => { salida += d; });
    hijo.on('close', fin);
    // Si el script muere de forma brusca, procesos huérfanos pueden mantener abiertas las tuberías: no se espera más de 1,5 s.
    hijo.on('exit', (codigo) => setTimeout(() => fin(codigo), 1500));
    alIniciar?.(hijo, () => salida);
  });
}

function matarFalsos(estadoDocker) {
  for (const f of readdirSync(estadoDocker).filter((n) => /^contenedor-.*\.json$/.test(n))) {
    try { process.kill(JSON.parse(readFileSync(join(estadoDocker, f), 'utf8')).pid, 'SIGKILL'); } catch { /* ya terminó */ }
  }
}

async function escenario({ zap = 'exito', backend = 'normal', extra = {}, args = [], alIniciar, entornoBackend = {} } = {}) {
  const servidor = crearBackendFalso({ modo: backend });
  const url = await servidor.escuchar();
  const base = mkdtempSync(join(tmpdir(), 'zap-prueba-'));
  const estadoDocker = mkdtempSync(join(tmpdir(), 'zap-docker-'));
  temporales.push(base, estadoDocker);
  secuencia += 1;
  const id = `prueba-${secuencia}`;
  const heredado = { ...process.env };
  for (const variable of ['JENKINS_URL', 'BUILD_NUMBER', 'BRANCH_NAME']) delete heredado[variable];
  const entorno = {
    ...heredado,
    PATH: `${FALSOS}${process.platform === 'win32' ? ';' : ':'}${process.env.PATH}`,
    MSYS_NO_PATHCONV: '1', MSYS2_ARG_CONV_EXCL: '*',
    FALSO_ESTADO: estadoDocker, ZAP_DIR_BASE: base, FALSO_ZAP_ESCENARIO: zap,
    ZAP_ID_EJECUCION: id, ZAP_SONDEO_SEGUNDOS: '1', ZAP_ESPERA_SEGUNDOS: '10', ZAP_LIMITE_SEGUNDOS: '60',
    IGUALAB_API_URL: url, IGUALAB_PRUEBAS_ENTORNO: 'qa', ZAP_CONFIRMO_DESTINO_QA: '127.0.0.1',
    IGUALAB_E2E_SUPERADMIN_CORREO: servidor.superadmin.correo, IGUALAB_E2E_SUPERADMIN_PASSWORD: servidor.superadmin.password,
    ...entornoBackend, ...extra,
  };
  const r = await ejecutar(args, entorno, { alIniciar });
  const resultados = join(base, 'zap-resultados');
  const leerJson = (f) => JSON.parse(readFileSync(join(resultados, f), 'utf8'));
  const acciones = existsSync(join(estadoDocker, 'acciones.log')) ? readFileSync(join(estadoDocker, 'acciones.log'), 'utf8') : '';
  return { ...r, servidor, id, base, resultados, acciones, entorno, estadoDocker, leerJson, cerrar: async () => { matarFalsos(estadoDocker); await servidor.cerrar(); } };
}

/** Todos los valores secretos que existieron en la ejecución (contraseñas y tokens). */
function secretosDe(e) {
  const usuarios = [...e.servidor.estado.usuarios.values()].map((u) => u.password);
  return [...usuarios, ...e.servidor.estado.sesiones.keys()].filter((s) => s && s.length >= 8);
}

function sinSecretos(e) {
  const todo = `${e.salida}\n${leerTodo(e.resultados)}`;
  for (const secreto of secretosDe(e)) assert.ok(!todo.includes(secreto), 'un secreto de la ejecución quedó en la consola o en los artefactos');
  assert.ok(!/eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\./.test(todo), 'quedó una cadena con forma de JWT');
}

function datosSinteticosCerrados(e) {
  const admin = [...e.servidor.estado.usuarios.values()].find((u) => u.rol === 'administrador');
  const empresa = [...e.servidor.estado.empresas.values()][0];
  assert.equal(admin?.habilitado, false, 'el Administrador sintético debe quedar deshabilitado');
  assert.equal(empresa?.activa, false, 'la empresa sintética debe quedar inactiva');
  assert.ok([...e.servidor.estado.sesiones.values()].every((s) => s.revocada), 'todas las sesiones de la ejecución deben quedar revocadas');
  assert.equal([...e.servidor.estado.usuarios.values()].find((u) => u.rol === 'superadmin').habilitado, true, 'el SuperAdmin no se toca');
}

describe('desenlaces del script oficial de ZAP', () => {
  test('0 · éxito → build verde, reportes redactados y datos sintéticos cerrados', async () => {
    const e = await escenario({ zap: 'exito' });
    try {
      assert.equal(e.codigo, 0, e.salida);
      for (const f of ['backend-zap.html', 'backend-zap.json', 'resumen-zap.md', 'resumen-zap.json', 'alcance-zap.json', 'consola-zap.log', 'cobertura-zap.json']) {
        assert.ok(existsSync(join(e.resultados, f)), `falta ${f}`);
      }
      const resumen = e.leerJson('resumen-zap.json');
      assert.equal(resumen.veredicto, 'EXITOSO');
      assert.equal(resumen.alcance.incluidas.length, 19);
      assert.equal(resumen.alcance.excluidas.length, 14);
      assert.ok(resumen.hook.cobertura_autenticada.demostrada);
      assert.match(readFileSync(join(e.resultados, 'resumen-zap.md'), 'utf8'), /PENDIENTE DE ACEPTACIÓN DEL TA/);
      sinSecretos(e);
      datosSinteticosCerrados(e);
    } finally { await e.cerrar(); }
  });

  test('el comando de ZAP: imagen fijada por digest, análisis activo (sin -S), reglas, hook y reportes', async () => {
    const e = await escenario({ zap: 'exito' });
    try {
      const linea = e.acciones.split('\n').find((l) => l.startsWith('run -d'));
      assert.ok(linea, 'no se lanzó ZAP');
      assert.match(linea, /zaproxy\/zap-stable:2\.17\.0@sha256:5ccfff9a411743ab2615136a2edd7416bec9ce41572fa94e935df38c14adb2b6/);
      assert.match(linea, /zap-api-scan\.py -t openapi-filtrado\.json -f openapi -c zap-api-scan\.reglas\.conf -r backend-zap\.html -J backend-zap\.json --hook=\/zap\/wrk\/zap_hooks\.py/);
      assert.ok(!/ -S( |$)/.test(linea), 'no debe usarse -S (baseline, sin análisis activo)');
      assert.ok(!/ -I( |$)/.test(linea), 'no debe usarse -I (ocultaría las advertencias)');
      assert.match(linea, /--network proxy_net/);
      assert.match(linea, /\/zap\/privado:ro/);
      assert.match(e.salida, /análisis ACTIVO habilitado; no se usa -S/);
    } finally { await e.cerrar(); }
  });

  test('2 · advertencias sin FAIL → INESTABLE (código 2)', async () => {
    const e = await escenario({ zap: 'advertencia' });
    try {
      assert.equal(e.codigo, 2, e.salida);
      assert.match(e.leerJson('resumen-zap.json').veredicto, /INESTABLE/);
      sinSecretos(e); datosSinteticosCerrados(e);
    } finally { await e.cerrar(); }
  });

  test('2 · con respuestas 5xx: el resumen lista método, ruta, request_id y metadatos, y NO archiva secretos', async () => {
    const e = await escenario({ zap: 'con-5xx' });
    try {
      assert.equal(e.codigo, 2, e.salida);
      const md = readFileSync(join(e.resultados, 'resumen-zap.md'), 'utf8');
      assert.match(md, /## Errores 5xx \(diagnóstico\)/);
      assert.match(md, /3f2b8c1e-5d4a-4e7b-9c0d-1a2b3c4d5e6f/, 'el request_id llega al resumen para buscarlo en el log del backend');
      assert.match(md, /Error inesperado/);
      const resumen = e.leerJson('resumen-zap.json');
      assert.equal(resumen.hook.errores_5xx_total, 2);
      assert.equal(resumen.hook.errores_5xx[0].cuerpo_con_nul, true);
      assert.equal(resumen.hook.errores_5xx[0].operacion, 'POST /auth/login');
      // La ruta del segundo diagnóstico traía algo con forma de JWT: ni el resumen, ni la cobertura, ni la consola pueden conservarlo.
      sinSecretos(e);
      assert.ok(!leerTodo(e.resultados).includes('firmaQueNoDebeArchivarse1234'));
      assert.ok(!/password/i.test(md), 'el resumen no incluye campos ni valores de cuerpos de petición');
      assert.ok(!md.includes('Bearer ey'), 'ni cabeceras de autenticación');
    } finally { await e.cerrar(); }
  });

  test('1 · reglas FAIL → build fallido (código 1) con los reportes archivados', async () => {
    const e = await escenario({ zap: 'fallo' });
    try {
      assert.equal(e.codigo, 1, e.salida);
      assert.ok(existsSync(join(e.resultados, 'backend-zap.json')));
      const alertas = e.leerJson('resumen-zap.json').alertas.alertas;
      assert.ok(alertas.some((a) => a.pluginid === '40018' && a.nivel_regla === 'FAIL'));
      sinSecretos(e); datosSinteticosCerrados(e);
      // La evidencia útil (URL, parámetro y ataque) se conserva; solo se sustituye el secreto.
      const json = readFileSync(join(e.resultados, 'backend-zap.json'), 'utf8');
      assert.match(json, /OR 1=1/);
      assert.match(json, /"uri": ?"http:\/\/127\.0\.0\.1:\d+\/empresas"/);
      assert.doesNotThrow(() => JSON.parse(json), 'el reporte redactado debe seguir siendo JSON válido');
      assert.match(json, /Authorization: \[REDACTADO\]/);
      assert.match(readFileSync(join(e.resultados, 'backend-zap.html'), 'utf8'), /Authorization: \[REDACTADO\]/);
      assert.ok(e.leerJson('resumen-zap.json').reportes.archivos.find((a) => a.nombre === 'backend-zap.json').sustituciones >= 1);
    } finally { await e.cerrar(); }
  });

  test('3 · error técnico de ZAP (código 3) → fallo, sin reportes que se presenten como éxito', async () => {
    const e = await escenario({ zap: 'tecnico' });
    try {
      assert.equal(e.codigo, 3, e.salida);
      assert.ok(!existsSync(join(e.resultados, 'backend-zap.json')));
      assert.match(e.leerJson('resumen-zap.json').veredicto, /FALLO TÉCNICO/);
      datosSinteticosCerrados(e);
    } finally { await e.cerrar(); }
  });

  test('7 · ZAP sale con 0 pero no generó los reportes → fallo técnico, nunca éxito', async () => {
    const e = await escenario({ zap: 'sin-reportes' });
    try {
      assert.equal(e.codigo, 7, e.salida);
      assert.match(e.leerJson('resumen-zap.json').motivo, /backend-zap\.html: no se generó/);
    } finally { await e.cerrar(); }
  });
});

describe('evidencia que debe existir para dar por válido el análisis', () => {
  const casos = [
    ['sin-hook', 9, /hook de ZAP no escribió/],
    ['sin-activo', 9, /análisis activo/],
    ['solo-401', 9, /acceso autenticado real/],
    ['alcance', 11, /operaciones excluidas/],
  ];
  for (const [zap, codigo, motivo] of casos) {
    test(`${zap} → código ${codigo}`, async () => {
      const e = await escenario({ zap });
      try {
        assert.equal(e.codigo, codigo, e.salida);
        assert.match(e.leerJson('resumen-zap.json').motivo, motivo);
      } finally { await e.cerrar(); }
    });
  }

  test('análisis activo parcial (límite de tiempo de ZAP) sin alertas → INESTABLE, no éxito', async () => {
    const e = await escenario({ zap: 'activo-parcial' });
    try {
      assert.equal(e.codigo, 2, e.salida);
      assert.match(e.leerJson('resumen-zap.json').veredicto, /análisis activo parcial/);
    } finally { await e.cerrar(); }
  });
});

describe('límite global y cancelación', () => {
  test('6 · se agota el tiempo → se elimina ÚNICAMENTE el contenedor de ZAP de la ejecución y se cierran los datos sintéticos', async () => {
    const e = await escenario({ zap: 'colgado', extra: { ZAP_LIMITE_SEGUNDOS: '4' } });
    try {
      assert.equal(e.codigo, 6, e.salida);
      const eliminados = e.acciones.split('\n').filter((l) => l.startsWith('REMOVIDO '));
      assert.deepEqual(eliminados, [`REMOVIDO igualab-zap-${e.id}`]);
      const objetivosDeRm = e.acciones.split('\n').filter((l) => l.startsWith('rm ')).map((l) => l.split(' ').at(-1));
      assert.ok(objetivosDeRm.every((n) => [`igualab-zap-${e.id}`, `igualab-zap-datos-${e.id}`].includes(n)), `docker rm solo sobre recursos de esta ejecución: ${objetivosDeRm}`);
      const volumenes = e.acciones.split('\n').filter((l) => l.startsWith('VOLUMEN-ELIMINADO '));
      assert.equal(volumenes.length, 3, 'se liberan los tres volúmenes de datos de la ejecución');
      assert.ok(volumenes.every((l) => l.endsWith(`-${e.id}`)), 'y solo los de esta ejecución');
      assert.match(e.leerJson('resumen-zap.json').motivo, /límite global/);
      datosSinteticosCerrados(e);
      sinSecretos(e);
    } finally { await e.cerrar(); }
  });

  test('cancelación brusca del stage → «limpiar» (post de Jenkins) elimina el contenedor y completa el cierre sintético', async () => {
    let enMarcha;
    const e = await escenario({
      zap: 'colgado', extra: { ZAP_LIMITE_SEGUNDOS: '600' },
      alIniciar: (hijo, salida) => {
        enMarcha = new Promise((resolver) => {
          const t = setInterval(() => {
            if (/ZAP API Scan \(análisis ACTIVO/.test(salida())) {
              clearInterval(t);
              setTimeout(() => { hijo.kill('SIGKILL'); resolver(); }, 2500); // el stage se cancela con ZAP en marcha
            }
          }, 100);
        });
      },
    });
    try {
      await enMarcha;
      assert.ok(existsSync(join(e.estadoDocker, `contenedor-igualab-zap-${e.id}.json`)), 'el contenedor de ZAP debe seguir vivo tras matar el script');
      assert.ok([...e.servidor.estado.usuarios.values()].some((u) => u.rol === 'administrador' && u.habilitado), 'antes del post, la cuenta sintética sigue habilitada');
      const limpieza = await ejecutar(['limpiar'], e.entorno);
      assert.equal(limpieza.codigo, 0, limpieza.salida);
      assert.ok(!existsSync(join(e.estadoDocker, `contenedor-igualab-zap-${e.id}.json`)), 'el contenedor de ZAP debe haberse eliminado');
      datosSinteticosCerrados(e);
      const segunda = await ejecutar(['limpiar'], e.entorno);
      assert.equal(segunda.codigo, 0);
      assert.ok(!/Cierre de emergencia/.test(segunda.salida), 'el cierre de emergencia es idempotente y no se repite');
    } finally { await e.cerrar(); }
  });

  test('si el cierre normal no pudo limpiar (código 10), el post reintenta SIN cambiar el veredicto original', async () => {
    const e = await escenario({ zap: 'exito', backend: 'desactivar-falla-una-vez' });
    try {
      assert.equal(e.codigo, 10, e.salida);
      assert.equal([...e.servidor.estado.empresas.values()][0].activa, true, 'la empresa sigue activa tras el primer cierre');
      const antes = readFileSync(join(e.resultados, 'resumen-zap.json'), 'utf8');
      const reintento = await ejecutar(['limpiar'], e.entorno);
      assert.equal(reintento.codigo, 0, reintento.salida);
      assert.match(reintento.salida, /Reintento de limpieza/);
      datosSinteticosCerrados(e);
      assert.equal(readFileSync(join(e.resultados, 'resumen-zap.json'), 'utf8'), antes, 'el resumen original no se sobrescribe');
      assert.equal(JSON.parse(antes).codigo_salida, 10);
    } finally { await e.cerrar(); }
  });

  test('«limpiar» no toca un contenedor que no lleva la etiqueta de esta ejecución', async () => {
    const e = await escenario({ zap: 'exito', args: ['limpiar'] });
    try {
      assert.equal(e.codigo, 0);
      assert.ok(!e.acciones.includes('REMOVIDO'));
    } finally { await e.cerrar(); }
  });
});

describe('destino, OpenAPI y autenticación (la preparación falla antes de lanzar ZAP)', () => {
  const sinZap = (e) => assert.ok(!e.acciones.includes('run -d'), 'ZAP no debe lanzarse');

  test('4 · el login del SuperAdmin falla → se detiene tras UN solo intento (no bloquea la cuenta)', async () => {
    const e = await escenario({ backend: 'login401' });
    try {
      assert.equal(e.codigo, 4, e.salida);
      assert.equal(e.servidor.estado.intentosLogin, 1);
      assert.match(e.salida, /no se reintenta/);
      sinZap(e);
    } finally { await e.cerrar(); }
  });

  test('4 · la credencial no es de un SuperAdmin → se rechaza y no se crea ninguna cuenta', async () => {
    const e = await escenario({ backend: 'rol-incorrecto' });
    try {
      assert.equal(e.codigo, 4, e.salida);
      assert.equal(e.servidor.estado.usuarios.size, 1);
      sinZap(e);
    } finally { await e.cerrar(); }
  });

  for (const [modo, patron] of [
    ['openapi-servidor-ajeno', /otros? destinos?|otro destino/],
    ['openapi-ref-remota', /\$ref no local/],
    ['operacion-nueva', /sin clasificar[\s\S]*GET \/nuevo-endpoint/],
  ]) {
    test(`4 · OpenAPI «${modo}» → se rechaza sin crear cuentas`, async () => {
      const e = await escenario({ backend: modo });
      try {
        assert.equal(e.codigo, 4, e.salida);
        assert.match(e.salida, patron);
        assert.equal(e.servidor.estado.usuarios.size, 1, 'no debe crearse ninguna cuenta antes de validar el OpenAPI');
        sinZap(e);
      } finally { await e.cerrar(); }
    });
  }

  test('5 · el backend no está disponible dentro del plazo acotado → código 5', async () => {
    const e = await escenario({ backend: 'sin-health', extra: { ZAP_ESPERA_SEGUNDOS: '3' } });
    try {
      assert.equal(e.codigo, 5, e.salida);
      sinZap(e);
    } finally { await e.cerrar(); }
  });

  test('4 · /openapi.json inaccesible → se rechaza', async () => {
    const e = await escenario({ backend: 'sin-openapi' });
    try {
      assert.ok([4, 5].includes(e.codigo), e.salida);
      sinZap(e);
    } finally { await e.cerrar(); }
  });

  test('4 · el entorno declarado no es qa → se rechaza', async () => {
    const e = await escenario({ extra: { IGUALAB_PRUEBAS_ENTORNO: 'uat' } });
    try { assert.equal(e.codigo, 4, e.salida); assert.match(e.salida, /exactamente "qa"/); sinZap(e); } finally { await e.cerrar(); }
  });

  test('4 · Jenkins fuera de la rama qa → se rechaza', async () => {
    const e = await escenario({ extra: { JENKINS_URL: 'http://jenkins.local', BRANCH_NAME: 'development' } });
    try { assert.equal(e.codigo, 4, e.salida); assert.match(e.salida, /solo se ejecuta en la rama qa/); sinZap(e); } finally { await e.cerrar(); }
  });

  test('4 · un host que parece UAT se rechaza aunque se confirme', async () => {
    const e = await escenario({ extra: { IGUALAB_API_URL: 'http://igualab-backend-uat-backend-1:8000', ZAP_CONFIRMO_DESTINO_QA: 'igualab-backend-uat-backend-1' } });
    try { assert.equal(e.codigo, 4, e.salida); assert.match(e.salida, /otro ambiente/); sinZap(e); } finally { await e.cerrar(); }
  });

  test('4 · un host que no identifica a QA exige confirmación explícita', async () => {
    const e = await escenario({ extra: { ZAP_CONFIRMO_DESTINO_QA: '' } });
    try { assert.equal(e.codigo, 4, e.salida); assert.match(e.salida, /ZAP_CONFIRMO_DESTINO_QA/); sinZap(e); } finally { await e.cerrar(); }
  });

  test('4 · el contenedor destino pertenece a otro proyecto Compose → se rechaza', async () => {
    const servidor = crearBackendFalso();
    const url = (await servidor.escuchar()).replace('127.0.0.1', 'localhost');
    await servidor.cerrar();
    const e = await escenario({
      extra: { IGUALAB_API_URL: url, ZAP_CONFIRMO_DESTINO_QA: 'localhost' },
      entornoBackend: { FALSO_CONTENEDOR_BACKEND: 'localhost', FALSO_PROYECTO_BACKEND: 'igualab-backend-uat' },
    });
    try { assert.equal(e.codigo, 4, e.salida); assert.match(e.salida, /no a 'igualab-backend-qa'/); sinZap(e); } finally { await e.cerrar(); }
  });

  test('4 · la red de Docker no existe → se rechaza; 3 · no se puede construir o descargar la imagen', async () => {
    const a = await escenario({ entornoBackend: { FALSO_SIN_RED: '1' } });
    try { assert.equal(a.codigo, 4, a.salida); } finally { await a.cerrar(); }
    const b = await escenario({ entornoBackend: { FALSO_BUILD_FALLA: '1' } });
    try { assert.equal(b.codigo, 3, b.salida); } finally { await b.cerrar(); }
    const c = await escenario({ entornoBackend: { FALSO_PULL_FALLA: '1' } });
    try { assert.equal(c.codigo, 3, c.salida); assert.match(c.salida, /digest/); } finally { await c.cerrar(); }
  });

  test('faltan credenciales → se informa el NOMBRE de la variable, nunca un valor', async () => {
    const e = await escenario({ extra: { IGUALAB_E2E_SUPERADMIN_PASSWORD: '' } });
    try {
      assert.equal(e.codigo, 4, e.salida);
      assert.match(e.salida, /IGUALAB_E2E_SUPERADMIN_PASSWORD/);
      assert.ok(!e.salida.includes(e.servidor.superadmin.password));
    } finally { await e.cerrar(); }
  });
});

describe('cuentas sintéticas y SuperAdmin', () => {
  test('la preparación usa UN login del SuperAdmin y UNO del Administrador sintético; el SuperAdmin nunca es destino de mutaciones', async () => {
    const e = await escenario({ zap: 'exito' });
    try {
      assert.equal(e.codigo, 0, e.salida);
      const mutaciones = e.servidor.estado.peticiones.filter((p) => ['POST', 'PATCH', 'PUT', 'DELETE'].includes(p.metodo));
      const permitidas = new Set(['POST /auth/login', 'POST /usuarios', 'POST /empresas', 'POST /auth/logout']);
      for (const p of mutaciones) {
        const ok = permitidas.has(`${p.metodo} ${p.ruta}`) || /^PATCH \/(empresas|usuarios)\/[^/]+\/(desactivar|deshabilitar)$/.test(`${p.metodo} ${p.ruta}`);
        assert.ok(ok, `mutación inesperada: ${p.metodo} ${p.ruta}`);
      }
      assert.ok(!mutaciones.some((p) => /transferir|habilitar$/.test(p.ruta) && !/deshabilitar$/.test(p.ruta)));
      assert.equal(e.servidor.estado.usuarios.size, 2, 'solo se crea UNA cuenta (la sintética); no se crea otro SuperAdmin');
      assert.equal([...e.servidor.estado.usuarios.values()].filter((u) => u.rol === 'superadmin').length, 1);
    } finally { await e.cerrar(); }
  });
});
