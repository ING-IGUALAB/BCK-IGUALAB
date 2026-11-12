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

// ---------------------------------------------------------------------------------------------------- interpretación del asistente

const CAMPOS_CONSULTA = [
  'respuesta', 'afirmaciones', 'citas', 'sin_evidencia', 'motivo', 'mensaje', 'afirmaciones_descartadas', 'estado_registrado', 'busqueda_documental',
  'interpretacion', 'interpretacion_descartada',
];
const guion = (prefijo) => peticiones(coleccion).find((p) => p.name.startsWith(prefijo)).event.find((e) => e.listen === 'test').script.exec.join('\n');

/** El ayudante TAL COMO VIAJA en la colección generada (no una copia): se extrae del script de F1 y se evalúa aquí. */
const problemasDeInterpretacion = (() => {
  const lineas = guion('F1 ').split('\n');
  const inicio = lineas.findIndex((l) => l.startsWith('const problemasDeInterpretacion = '));
  const fin = lineas.findIndex((l, i) => i > inicio && l === '};');
  assert.ok(inicio >= 0 && fin > inicio, 'F1 lleva el ayudante de interpretación');
  return new Function(`${lineas.slice(inicio, fin + 1).join('\n')}\nreturn problemasDeInterpretacion;`)();
})();

const consulta = (cambios = {}) => ({
  respuesta: 'El consumo energético se reporta según GRI 302-1. [1]',
  afirmaciones: [{ texto: 'El consumo energético se reporta según GRI 302-1.', referencias: [1], cita_textual: 'se reporta según GRI 302-1' }],
  citas: [{ referencia: 1, seccion: 'Energía › 3.2 Consumo' }],
  sin_evidencia: false, motivo: null, mensaje: null, afirmaciones_descartadas: 0, estado_registrado: null, busqueda_documental: 'REALIZADA',
  interpretacion: null, interpretacion_descartada: false,
  ...cambios,
});
const sinEvidencia = (cambios = {}) =>
  consulta({ respuesta: '', afirmaciones: [], citas: [], sin_evidencia: true, motivo: 'MODELO_SIN_EVIDENCIA', mensaje: 'No se encontró evidencia.', ...cambios });

test('F1, F1d y F1e exigen el contrato de 11 campos de ConsultaResponse (no aceptan campos de más ni de menos)', () => {
  for (const prefijo of ['F1 ', 'F1d ', 'F1e ']) {
    const codigo = guion(prefijo);
    assert.match(codigo, /Object\.keys\(j\)\)\.to\.have\.members\(a\.campos\)/, prefijo);
    assert.ok(codigo.includes(JSON.stringify({ campos: CAMPOS_CONSULTA })), `${prefijo}compara con los 11 campos`);
  }
  assert.match(guion('F1d '), /j\.interpretacion\)\.to\.equal\(null\)/);
  assert.match(guion('F1d '), /j\.interpretacion_descartada\)\.to\.equal\(false\)/);
  for (const prefijo of ['F1 ', 'F1e ']) assert.match(guion(prefijo), /problemasDeInterpretacion\(j, a\.pregunta\)\.join\(' \| '\)\)\.to\.equal\(''\)/, prefijo);
  assert.match(guion('F1e '), /la cita textual figura en el documento de B/);
});

test('la interpretación es opcional y, si existe, acompaña a afirmaciones con cita', () => {
  const pregunta = '¿Alcanza la meta de 20 %?';
  assert.deepEqual(problemasDeInterpretacion(consulta(), pregunta), []); // respuesta factual: sin interpretación
  assert.deepEqual(problemasDeInterpretacion(sinEvidencia(), pregunta), []);
  assert.deepEqual(problemasDeInterpretacion(consulta({ interpretacion_descartada: true }), pregunta), []);
  const interpretada = consulta({ interpretacion: 'Reportar según GRI 302-1 en la sección 3.2 sugiere trazabilidad; frente a la meta de 20 % falta evidencia en 2026.' });
  assert.deepEqual(problemasDeInterpretacion(interpretada, pregunta), []); // cifras de la cita, la sección y la pregunta; los años no cuentan
});

test('una interpretación sin respaldo, mezclada con las citas o con cifras ajenas se detecta', () => {
  const falla = (respuesta, fragmento) => {
    const problemas = problemasDeInterpretacion(respuesta, '¿Qué sugiere el reporte?');
    assert.ok(problemas.some((p) => p.includes(fragmento)), `${fragmento} ← ${JSON.stringify(problemas)}`);
  };
  falla(sinEvidencia({ interpretacion: 'Sugiere una buena gestión.' }), 'sin evidencia no trae interpretación');
  falla(sinEvidencia({ interpretacion_descartada: true }), 'sin evidencia no trae interpretación');
  falla(consulta({ interpretacion: 'Sugiere una buena gestión.', afirmaciones: [], citas: [] }), 'solo puede acompañar a afirmaciones con cita');
  falla(consulta({ interpretacion: 'Sugiere una buena gestión.', busqueda_documental: 'NO_REALIZADA' }), 'solo puede acompañar a afirmaciones con cita');
  falla(consulta({ interpretacion: 'Sugiere una buena gestión.', interpretacion_descartada: true }), 'descartada no se muestra');
  falla(consulta({ interpretacion: 'El ahorro real fue de 35 %.' }), 'introduce una cifra');
  falla(consulta({ interpretacion: 'Sugiere una buena gestión [1].' }), 'marcadores [n]');
  falla(consulta({ interpretacion: 'se reporta según GRI 302-1', respuesta: 'El consumo se reporta según GRI 302-1. [1]' }), 'mezclada con el texto citado');
  for (const invalida of ['', '   ', 7, ['x'], 'x'.repeat(4001)]) falla(consulta({ interpretacion: invalida }), 'no es null ni un texto no vacío');
  falla(consulta({ interpretacion_descartada: 'no' }), 'no es booleano');
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
