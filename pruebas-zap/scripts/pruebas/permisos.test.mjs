/**
 * Regresión del fallo de Jenkins QA build 36 (preparación y cierre terminan con «Error (EACCES)» y no queda resumen).
 *   node --test scripts/pruebas/permisos.test.mjs
 *
 * Parte portable: diagnóstico con fase/operación/ruta/código, preparación que falla antes de tocar nada y cierre que tolera una
 * preparación incompleta y deja siempre un resumen. Parte con Docker real (solo Linux con Docker): pruebas/permisos-real.sh, que
 * reproduce el caso con un uid no root y directorios «creados por otro contenedor».
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { after, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { SALIDA } from '../lib/codigos.mjs';
import { comprobarEscritura, conDiagnostico, esErrorDeArchivo, mensajeDeArchivo } from '../lib/diagnostico.mjs';
import { decidirSalida } from '../lib/decision.mjs';

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const temporales = [];
const carpeta = () => { const d = mkdtempSync(join(tmpdir(), 'zap-permisos-')); temporales.push(d); return d; };
after(() => { for (const t of temporales) { try { chmodSync(t, 0o700); } catch { /* ya no existe */ } rmSync(t, { recursive: true, force: true }); } });

const erroresDe = (codigo, syscall = 'open') => Object.assign(new Error('mensaje interno con /ruta/ajena'), { code: codigo, syscall, path: '/privado/estado.json' });
const esRootOWindows = process.platform === 'win32' || process.getuid?.() === 0;

function ejecutarNode(script, argumentos, entorno = {}) {
  const r = spawnSync(process.execPath, [join(RAIZ, 'scripts', script), ...argumentos], {
    encoding: 'utf8', env: { PATH: process.env.PATH, ...entorno },
  });
  return { codigo: r.status, salida: `${r.stdout}${r.stderr}` };
}

describe('diagnóstico de errores de archivo', () => {
  test('un error de E/S se convierte en un mensaje con fase, operación, ruta, código e identidad (sin el mensaje interno)', () => {
    const e = assert.throws(() => conDiagnostico('estado', 'escribir', '/privado/estado.json', () => { throw erroresDe('EACCES'); }), (error) => {
      assert.equal(error.codigoSalida, SALIDA.ARCHIVOS);
      assert.match(error.message, /^\[fase: estado\] escribir \/privado\/estado\.json falló con EACCES \(uid=/);
      assert.ok(!error.message.includes('ruta/ajena'), 'no reenvía el mensaje interno del error');
      return true;
    });
    assert.equal(e, undefined);
  });

  test('un error que no es de archivo pasa sin transformarse', () => {
    const original = new TypeError('otro problema');
    assert.throws(() => conDiagnostico('estado', 'escribir', '/x', () => { throw original; }), (error) => error === original);
    assert.equal(esErrorDeArchivo(original), false);
    assert.equal(esErrorDeArchivo(erroresDe('EACCES')), true);
  });

  test('el mensaje nunca contiene contenido: solo fase, operación, ruta del contenedor, código y dueño', () => {
    const m = mensajeDeArchivo('montajes', 'open', '/privado/.sonda', erroresDe('EACCES'));
    assert.match(m, /\[fase: montajes\] open \/privado\/\.sonda falló con EACCES \(uid=\S+ gid=\S+; /);
  });

  test('comprobarEscritura: un directorio inexistente se informa con la fase y el código', () => {
    const inexistente = join(carpeta(), 'no-existe');
    assert.throws(() => comprobarEscritura('montajes', [inexistente]), (error) => {
      assert.equal(error.codigoSalida, SALIDA.ARCHIVOS);
      assert.match(error.message, /\[fase: montajes\].*ENOENT/);
      return true;
    });
  });

  test('comprobarEscritura: un directorio de solo lectura da EACCES con el propietario y el modo', { skip: esRootOWindows && 'requiere Linux y un usuario no root' }, () => {
    const d = carpeta();
    chmodSync(d, 0o555);
    assert.throws(() => comprobarEscritura('montajes', [d]), (error) => {
      assert.match(error.message, /EACCES/);
      assert.match(error.message, /modo=555/);
      return true;
    });
  });

  test('un directorio escribible pasa y no deja la sonda', () => {
    const d = carpeta();
    comprobarEscritura('montajes', [d]);
    assert.equal(readdirSyncSeguro(d).length, 0);
  });

  test('el código 12 de la preparación se conserva en el veredicto (no se confunde con «error de ZAP»)', () => {
    const r = decidirSalida({ prepCodigo: SALIDA.ARCHIVOS, zapEjecutado: false, zapCodigo: null, tiempoAgotado: false, reportes: { ok: true }, secretos: { ok: true }, hook: null, limpieza: { ok: true } });
    assert.equal(r.codigo, SALIDA.ARCHIVOS);
    assert.match(r.veredicto, /FALLO TÉCNICO/);
  });
});

function readdirSyncSeguro(d) {
  return spawnSync(process.execPath, ['-e', `console.log(JSON.stringify(require('fs').readdirSync(${JSON.stringify(d)})))`], { encoding: 'utf8' }).stdout.trim() === '[]' ? [] : ['x'];
}

describe('preparación: falla ANTES de tocar el backend', () => {
  test('con un directorio de datos inutilizable sale con 12, fase «montajes», sin contactar al backend ni crear nada', () => {
    const trabajo = carpeta();
    const privado = join(carpeta(), 'no-existe');
    const r = ejecutarNode('preparar.mjs', ['--trabajo', trabajo, '--privado', privado, '--id', 'regresion'], {
      IGUALAB_E2E_SUPERADMIN_PASSWORD: 'Secreto-Que-No-Debe-Salir-1!',
    });
    assert.equal(r.codigo, SALIDA.ARCHIVOS, r.salida);
    assert.match(r.salida, /\[fase: montajes\].*\.sonda.*ENOENT/);
    assert.ok(!r.salida.includes('Alcance:'), 'no llegó a analizar el OpenAPI');
    assert.ok(!r.salida.includes('Secreto-Que-No-Debe-Salir'));
    assert.ok(!r.salida.includes('Error (EACCES)'));
  });
});

describe('cierre: tolera una preparación incompleta y deja siempre un resumen', () => {
  const argumentosBase = (d, extra = []) => [
    '--trabajo', d.trabajo, '--privado', d.privado, '--resultados', d.resultados, '--id', 'regresion',
    '--prep-codigo', '4', '--zap-codigo', 'ninguno', '--tiempo-agotado', '0', '--limite-minutos', '80', '--imagen', 'img', '--comando', 'c', ...extra,
  ];
  const nuevo = () => { const base = carpeta(); const d = { trabajo: join(base, 't'), privado: join(base, 'p'), resultados: join(base, 'r') }; Object.values(d).forEach((x) => mkdirSync(x)); return d; };

  test('sin estado privado (la preparación ni llegó a crearlo): resumen de fallo y «nada que limpiar» en consola', () => {
    const d = nuevo();
    const r = ejecutarNode('finalizar.mjs', argumentosBase(d));
    assert.equal(r.codigo, 4, r.salida);
    assert.match(r.salida, /No se crearon recursos sintéticos ni sesiones que cerrar/);
    for (const f of ['resumen-zap.json', 'resumen-zap.md', 'alcance-zap.json']) assert.ok(existsSync(join(d.resultados, f)), f);
    assert.equal(JSON.parse(readFileSync(join(d.resultados, 'resumen-zap.json'), 'utf8')).veredicto, 'FALLO TÉCNICO');
  });

  test('estado privado truncado: lo informa, NO da la limpieza por buena y deja el resumen con qué revisar', () => {
    const d = nuevo();
    writeFileSync(join(d.privado, 'estado.json'), '{"id_ejecucion": "regresion", "usuario_id": "abc'); // la escritura se interrumpió
    const r = ejecutarNode('finalizar.mjs', argumentosBase(d));
    assert.notEqual(r.codigo, 0);
    assert.match(r.salida, /estado\.json existe pero no es JSON válido/);
    assert.match(r.salida, /No se pudo comprobar si quedaron recursos sintéticos/);
    const resumen = JSON.parse(readFileSync(join(d.resultados, 'resumen-zap.json'), 'utf8'));
    assert.equal(resumen.limpieza.ok, false);
    assert.ok(resumen.limpieza.pasos.some((p) => /zap-regresion@igualab-zap\.example\.com/.test(p.detalle ?? '')), 'indica la cuenta y la empresa a revisar');
  });

  test('/resultados no existe: informa fase, ruta y código y muestra el resumen por consola; la causa principal (4) manda en el código', () => {
    const d = nuevo();
    d.resultados = join(carpeta(), 'sin-resultados');
    const r = ejecutarNode('finalizar.mjs', argumentosBase(d));
    assert.equal(r.codigo, 4, r.salida); // el fallo de configuración de la preparación sigue siendo el primario
    assert.match(r.salida, /\[fase: cierre\].*\.sonda.*ENOENT/);
    assert.match(r.salida, /se muestra aquí/);
    assert.match(r.salida, /# OWASP ZAP/);
    assert.match(r.salida, /FALLO TÉCNICO/);
  });

  test('si la causa principal era un éxito (0/1/2) pero no se pueden escribir los artefactos, el código pasa a 12', () => {
    const d = nuevo();
    d.resultados = join(carpeta(), 'sin-resultados');
    // ZAP «terminó bien» con 0, pero no hay dónde dejar los reportes: un éxito no es creíble.
    const r = ejecutarNode('finalizar.mjs', argumentosBase(d).map((x, i, a) => (a[i - 1] === '--prep-codigo' ? '0' : x)));
    assert.notEqual(r.codigo, 0, r.salida);
    assert.match(r.salida, /\[fase: cierre\].*ENOENT/);
  });

  test('un archivo previo de otro propietario en resultados se reemplaza, no se sobrescribe', { skip: esRootOWindows && 'requiere Linux y un usuario no root' }, () => {
    const d = nuevo();
    writeFileSync(join(d.resultados, 'consola-zap.log'), 'consola previa\n', { mode: 0o444 }); // solo lectura, como el copiado por `docker cp`
    const r = ejecutarNode('finalizar.mjs', argumentosBase(d));
    assert.equal(r.codigo, 4, r.salida);
    assert.equal(readFileSync(join(d.resultados, 'consola-zap.log'), 'utf8'), 'consola previa\n');
    assert.ok(existsSync(join(d.resultados, 'resumen-zap.md')));
  });
});

describe('regresión con Docker real (Linux)', () => {
  const hayDocker = process.platform === 'linux' && spawnSync('docker', ['info'], { stdio: 'ignore' }).status === 0;
  test('pruebas/permisos-real.sh: uid no root, directorios creados por otro contenedor y volúmenes nombrados', { skip: !hayDocker && 'requiere Linux con Docker (en Windows: ejecutar con WSL, ver README)', timeout: 280_000 }, () => {
    const r = spawnSync('sh', [join(RAIZ, 'pruebas', 'permisos-real.sh')], { encoding: 'utf8' });
    assert.equal(r.status, 0, `${r.stdout}\n${r.stderr}`);
    assert.match(r.stdout, /REGRESIÓN DE PERMISOS: todo correcto/);
  });
});
