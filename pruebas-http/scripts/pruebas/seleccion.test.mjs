/**
 * Comprobaciones ESTÁTICAS del orden y las dependencias de la colección E2E y de la resolución de selecciones del ejecutor (H07).
 * No envían ninguna petición ni necesitan backend: leen el JSON versionado y ejecutan `resolverSeleccion`.
 * Que estas pruebas pasen NO demuestra que los flujos pasen contra un backend: eso solo lo demuestra una ejecución real.
 *
 *   npm run probar-ejecutor     (node --test scripts/pruebas/seleccion.test.mjs)
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { ErrorDeEjecucion, SALIDA } from '../lib/configuracion.mjs';
import { letraDeCarpeta, resolverSeleccion } from '../lib/seleccion.mjs';

const raiz = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const coleccion = JSON.parse(readFileSync(join(raiz, 'colecciones', 'igualab-e2e.postman_collection.json'), 'utf8'));
const carpetas = coleccion.item.filter((n) => !n.request);
const letras = carpetas.map((c) => letraDeCarpeta(c.name));
const mapa = (nombre) => JSON.parse(coleccion.variable.find((v) => v.key === nombre).value);
const requisitos = mapa('requisitos_carpetas');
const cierres = mapa('cierres_carpetas');

const peticiones = (nodo) => (nodo.request ? [nodo] : (nodo.item ?? []).flatMap(peticiones));
const texto = (nodo) => JSON.stringify(nodo);
const consumeB = (carpeta) => /\bb_[a-z0-9_]+/.test(texto(carpeta));
const esCierreDeB = (p) =>
  p.request.method === 'PATCH' && /\{\{b_empresa_id\}\}\/desactivar|\{\{b_admin_id\}\}\/deshabilitar/.test(p.request.url);

// ---------------------------------------------------------------------------------------------------- orden y dependencias

test('los siete flujos siguen en su orden y el cierre de B va al final', () => {
  assert.deepEqual(letras, ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'Z']);
});

test('la colección no trae datos de otra ejecución: solo la URL base y los dos mapas de selección', () => {
  assert.deepEqual(coleccion.variable.map((v) => v.key).sort(), ['base_url', 'cierres_carpetas', 'requisitos_carpetas']);
});

test('el cierre lógico de B (desactivar su empresa, deshabilitar a su Administrador) existe solo en la carpeta Z', () => {
  const cierres_ = carpetas.flatMap((c) => peticiones(c).filter(esCierreDeB).map((p) => `${letraDeCarpeta(c.name)}:${p.name}`));
  assert.equal(cierres_.length, 2, cierres_.join(' | '));
  assert.ok(cierres_.every((c) => c.startsWith('Z:')), `cierre fuera de Z: ${cierres_.join(' | ')}`);
});

test('ninguna petición que use los datos de B va después de su cierre, salvo la comprobación de que la sesión quedó revocada', () => {
  const todas = peticiones(coleccion).map((p) => ({ nombre: p.name, usaB: consumeB(p), cierre: esCierreDeB(p) }));
  const primerCierre = todas.findIndex((p) => p.cierre);
  assert.ok(primerCierre > 0);
  const posteriores = todas.slice(primerCierre + 1).filter((p) => p.usaB).map((p) => p.nombre);
  assert.deepEqual(posteriores, ['Z2 Cierre: deshabilitar al Administrador de B', 'Z3 La sesión del Administrador de B quedó revocada → 401']);
  assert.ok(!todas.slice(0, primerCierre).some((p) => p.nombre.startsWith('Z')));
});

test('las carpetas que usan datos de B declaran a B como requisito, y solo ellas', () => {
  const usan = carpetas.filter((c) => letraDeCarpeta(c.name) !== 'B' && consumeB(c)).map((c) => letraDeCarpeta(c.name));
  assert.deepEqual(usan, ['C', 'E', 'F', 'G', 'Z']);
  assert.deepEqual(Object.keys(requisitos).sort(), usan);
  for (const letra of usan) assert.deepEqual(requisitos[letra], ['B']);
});

test('A y D son independientes: no usan datos de B ni declaran requisitos', () => {
  for (const letra of ['A', 'D']) {
    const carpeta = carpetas.find((c) => letraDeCarpeta(c.name) === letra);
    assert.equal(consumeB(carpeta), false, `${letra} usa datos de B`);
    assert.equal(requisitos[letra], undefined);
  }
});

test('cada requisito está antes que quien lo necesita y el cierre de B va después de todos sus consumidores', () => {
  for (const [letra, necesarias] of Object.entries(requisitos)) {
    for (const necesaria of necesarias) assert.ok(letras.indexOf(necesaria) < letras.indexOf(letra), `${necesaria} debe ir antes que ${letra}`);
  }
  const posicionCierre = letras.indexOf(cierres.B);
  assert.equal(posicionCierre, letras.length - 1);
  for (const letra of Object.keys(requisitos).filter((l) => l !== cierres.B)) assert.ok(letras.indexOf(letra) < posicionCierre);
});

test('C conserva el duplicado con exactamente los mismos bytes: usa c-copia.md, que el ejecutor deja idéntico a b-reporte.md', () => {
  const c = carpetas.find((x) => letraDeCarpeta(x.name) === 'C');
  assert.match(texto(c), /muestras\/c-copia\.md/);
  assert.match(texto(c), /DOCUMENT_ALREADY_INGESTED/);
  const muestras = readFileSync(join(raiz, 'scripts', 'lib', 'muestras.mjs'), 'utf8');
  assert.match(muestras, /Las muestras de B y C deben tener exactamente los mismos bytes/);
});

test('A7 y Z3 conservan la expectativa 401 de una sesión revocada, y G3 el 403 de una sesión válida con rol incorrecto', () => {
  const por = (prefijo) => peticiones(coleccion).find((p) => p.name.startsWith(prefijo));
  assert.match(texto(por('A7 ')), /sinSesion|INVALID_SESSION/);
  assert.match(texto(por('Z3 ')), /INVALID_SESSION/);
  assert.match(texto(por('G3 ')), /FORBIDDEN/);
  assert.match(por('G3 ').request.header.map((h) => h.value).join(), /b_token_admin/);
});

test('el caso positivo del RAG no acepta «sin evidencia» y la ausencia de evidencia tiene su propio caso determinista', () => {
  const f1 = texto(peticiones(coleccion).find((p) => p.name.startsWith('F1 ')));
  assert.match(f1, /sin_evidencia[^"]*\)\.to\.equal\(false\)/);
  assert.doesNotMatch(f1, /if \(j\.sin_evidencia\)/);
  const f1d = texto(peticiones(coleccion).find((p) => p.name.startsWith('F1d ')));
  assert.match(f1d, /SIN_FRAGMENTOS/);
});

// ---------------------------------------------------------------------------------------------------- selecciones

const nombres = (letrasPedidas) => letrasPedidas.map((l) => carpetas.find((c) => letraDeCarpeta(c.name) === l).name);

const VALIDAS = [
  [[], [], []],
  [['A'], ['A'], []],
  [['D'], ['D'], []],
  [['A', 'D'], ['A', 'D'], []],
  [['D', 'A'], ['A', 'D'], []],
  [['B'], ['B', 'Z'], ['Z']],
  [['B', 'C'], ['B', 'C', 'Z'], ['Z']],
  [['C', 'B'], ['B', 'C', 'Z'], ['Z']],
  [['B', 'E'], ['B', 'E', 'Z'], ['Z']],
  [['B', 'F'], ['B', 'F', 'Z'], ['Z']],
  [['B', 'G'], ['B', 'G', 'Z'], ['Z']],
  [['G', 'E', 'B'], ['B', 'E', 'G', 'Z'], ['Z']],
  [['B', 'E', 'F', 'G'], ['B', 'E', 'F', 'G', 'Z'], ['Z']],
  [['A', 'B', 'C', 'D', 'E', 'F', 'G'], ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'Z'], ['Z']],
  [['B', 'Z'], ['B', 'Z'], []],
  [['A', 'B', 'Z', 'D'], ['A', 'B', 'D', 'Z'], []],
];

for (const [pedidas, esperadas, agregadas] of VALIDAS) {
  test(`selección ${JSON.stringify(pedidas)} → ${esperadas.join('+') || 'colección completa'}`, () => {
    const resultado = resolverSeleccion(coleccion, pedidas);
    assert.deepEqual(resultado.carpetas.map(letraDeCarpeta), esperadas);
    assert.deepEqual(resultado.agregadas.map(letraDeCarpeta), agregadas);
  });
}

test('la colección completa (sin --carpeta) no filtra nada y su orden efectivo es el de la colección', () => {
  assert.deepEqual(resolverSeleccion(coleccion, []), { carpetas: [], agregadas: [] });
  assert.deepEqual(letras, ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'Z']);
});

test('--carpeta acepta el nombre completo y no repite una carpeta pedida dos veces', () => {
  assert.deepEqual(resolverSeleccion(coleccion, [nombres(['B'])[0], 'B']).carpetas.map(letraDeCarpeta), ['B', 'Z']);
});

const INVALIDAS = [['C'], ['E'], ['F'], ['G'], ['Z'], ['A', 'C'], ['D', 'E'], ['A', 'D', 'F'], ['C', 'E', 'G'], ['Z', 'A']];

for (const pedidas of INVALIDAS) {
  test(`selección ${JSON.stringify(pedidas)} falla antes de enviar nada, indicando que falta B`, () => {
    assert.throws(
      () => resolverSeleccion(coleccion, pedidas),
      (error) => error instanceof ErrorDeEjecucion && error.codigoSalida === SALIDA.CONFIGURACION && /necesita los datos de B/.test(error.message),
    );
  });
}

test('una carpeta inexistente o ambigua falla con la lista de las disponibles', () => {
  assert.throws(() => resolverSeleccion(coleccion, ['X']), (e) => e instanceof ErrorDeEjecucion && /Disponibles/.test(e.message));
});

test('metadatos de selección corruptos se rechazan en lugar de ignorarse', () => {
  const rota = { ...coleccion, variable: [{ key: 'requisitos_carpetas', value: '[1,2]' }] };
  assert.throws(() => resolverSeleccion(rota, ['A']), (e) => e instanceof ErrorDeEjecucion && /no es un objeto JSON válido/.test(e.message));
});

test('las colecciones sin metadatos (integración) siguen aceptando cualquier carpeta sin añadir nada', () => {
  const integracion = JSON.parse(readFileSync(join(raiz, 'colecciones', 'igualab-integracion.postman_collection.json'), 'utf8'));
  const primera = integracion.item.find((n) => !n.request).name;
  assert.deepEqual(resolverSeleccion(integracion, [primera]), { carpetas: [primera], agregadas: [] });
});
