import assert from 'node:assert/strict';
import { test } from 'node:test';

import { diagnosticoDeRespuesta, lineaDeDiagnostico } from '../lib/resultados.mjs';

const RID = '0f8fad5b-d9cb-469f-a165-70867728950e';
const json = (objeto) => Buffer.from(JSON.stringify(objeto));
const cabeceras = (mapa) => (nombre) => mapa[nombre.toLowerCase()];

test('conserva código, mensaje, request_id y detalles del contrato de errores', () => {
  const d = diagnosticoDeRespuesta({
    cuerpo: json({ error: { code: 'EMBEDDING_PROVIDER_ERROR', message: 'El proveedor de embeddings devolvió un error.', details: { proveedor: 'oci', lote: 0, tipo_error: 'ServiceError' }, request_id: RID } }),
    cabeceras: cabeceras({ 'content-type': 'application/json', 'x-request-id': RID }),
  });
  assert.equal(d.codigo, 'EMBEDDING_PROVIDER_ERROR');
  assert.equal(d.mensaje, 'El proveedor de embeddings devolvió un error.');
  assert.equal(d.request_id, RID);
  assert.deepEqual(d.detalles, { proveedor: 'oci', lote: 0, tipo_error: 'ServiceError' });
  assert.match(lineaDeDiagnostico(d), /request_id=0f8fad5b.*código=EMBEDDING_PROVIDER_ERROR/);
});

test('toma el request_id del cuerpo si falta la cabecera y descarta el que no es un UUID', () => {
  const cuerpo = json({ error: { code: 'X', message: 'm', request_id: RID } });
  assert.equal(diagnosticoDeRespuesta({ cuerpo, cabeceras: cabeceras({ 'content-type': 'application/json' }) }).request_id, RID);
  const falso = json({ error: { code: 'X', message: 'm', request_id: 'no-es-uuid' } });
  assert.equal(diagnosticoDeRespuesta({ cuerpo: falso, cabeceras: cabeceras({ 'content-type': 'application/json' }) }).request_id, undefined);
});

test('omite claves sensibles, valores con forma de token y recorta textos largos', () => {
  const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop';
  const d = diagnosticoDeRespuesta({
    cuerpo: json({ error: { code: 'X', message: `fallo con Bearer ${'a'.repeat(40)}`, details: { access_token: 'abc', password: 'p', anidado: { cookie: 'c', nota: jwt }, contenido: '# documento completo', largo: 'z'.repeat(500), lista: Array.from({ length: 30 }, (_, i) => i) } } }),
    cabeceras: cabeceras({ 'content-type': 'application/json' }),
  });
  const texto = JSON.stringify(d);
  assert.equal(d.mensaje, '[omitido]');
  assert.equal(d.detalles.access_token, '[omitido]');
  assert.equal(d.detalles.password, '[omitido]');
  assert.equal(d.detalles.contenido, '[omitido]');
  assert.equal(d.detalles.anidado.cookie, '[omitido]');
  assert.equal(d.detalles.anidado.nota, '[omitido]');
  assert.ok(d.detalles.largo.length < 200);
  assert.equal(d.detalles.lista.length, 11);
  assert.ok(!texto.includes(jwt) && !texto.includes('documento completo'));
});

test('limita el tamaño total de los detalles', () => {
  const details = Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`campo_${i}`, 'x'.repeat(150)]));
  const d = diagnosticoDeRespuesta({ cuerpo: json({ error: { code: 'X', message: 'm', details } }), cabeceras: cabeceras({ 'content-type': 'application/json' }) });
  assert.equal(d.detalles.truncado, true);
  assert.ok(d.detalles.inicio.length <= 1200);
});

test('no copia un cuerpo que no es el contrato de errores (HTML de un proxy, JSON de éxito, cuerpo enorme)', () => {
  const html = diagnosticoDeRespuesta({ cuerpo: Buffer.from('<html>502 Bad Gateway secreto</html>'), cabeceras: cabeceras({ 'content-type': 'text/html; charset=utf-8' }) });
  assert.deepEqual(html, { tipo_contenido: 'text/html', bytes_cuerpo: 36 });
  assert.match(lineaDeDiagnostico(html), /sin el contrato de errores/);
  const exito = diagnosticoDeRespuesta({ cuerpo: json({ access_token: 'abc', estado: 'COMPLETADO' }), cabeceras: cabeceras({ 'content-type': 'application/json' }) });
  assert.deepEqual(exito, { tipo_contenido: 'application/json', bytes_cuerpo: exito.bytes_cuerpo, sin_contrato_de_error: true });
  const enorme = diagnosticoDeRespuesta({ cuerpo: Buffer.alloc(70_000, 0x20), cabeceras: cabeceras({ 'content-type': 'application/json' }) });
  assert.deepEqual(enorme, { tipo_contenido: 'application/json', bytes_cuerpo: 70_000 });
  const roto = diagnosticoDeRespuesta({ cuerpo: Buffer.from('{"error":'), cabeceras: cabeceras({ 'content-type': 'application/json' }) });
  assert.equal(roto.cuerpo_no_interpretable, true);
  const vacio = diagnosticoDeRespuesta({ cuerpo: undefined });
  assert.deepEqual(vacio, { tipo_contenido: 'desconocido', bytes_cuerpo: 0 });
});
