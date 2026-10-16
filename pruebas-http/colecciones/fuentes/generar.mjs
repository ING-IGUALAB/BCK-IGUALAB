/**
 * Genera las colecciones Postman (JSON v2.1) a partir de las fuentes de esta carpeta.
 *
 *   node colecciones/fuentes/generar.mjs             escribe los dos JSON de `colecciones/`
 *   node colecciones/fuentes/generar.mjs --verificar falla si los JSON versionados no coinciden con las fuentes
 *
 * Además valida: sintaxis de cada script, nombres de petición únicos, aserciones en cada petición y ausencia de `console.log`.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

import { contarPeticiones } from './dsl.mjs';
import { coleccionE2E } from './e2e.mjs';
import { coleccionIntegracion } from './integracion.mjs';

const aqui = dirname(fileURLToPath(import.meta.url));
const destino = join(aqui, '..');
const SALIDAS = [
  ['igualab-integracion.postman_collection.json', coleccionIntegracion],
  ['igualab-e2e.postman_collection.json', coleccionE2E],
];

function* peticiones(nodo, ruta = []) {
  if (nodo.request) {
    yield { nodo, ruta };
    return;
  }
  for (const hijo of nodo.item ?? []) yield* peticiones(hijo, [...ruta, nodo.name]);
}

function validar(nombre, coleccion) {
  const nombres = new Set();
  const problemas = [];
  for (const { nodo, ruta } of peticiones(coleccion)) {
    const id = [...ruta, nodo.name].join(' / ');
    if (nombres.has(id)) problemas.push(`petición duplicada: ${id}`);
    nombres.add(id);
    const prueba = nodo.event.find((e) => e.listen === 'test');
    if (!prueba || !prueba.script.exec.join('\n').includes('pm.test(') && !prueba.script.exec.join('\n').includes('errorContrato(')) {
      problemas.push(`sin aserciones: ${id}`);
    }
    for (const evento of nodo.event) {
      const codigo = evento.script.exec.join('\n');
      try {
        new vm.Script(codigo, { filename: id });
      } catch (error) {
        problemas.push(`sintaxis inválida en «${id}» (${evento.listen}): ${error.message}`);
      }
      if (/console\.(log|dir|info|warn|error)/.test(codigo)) problemas.push(`console.* en «${id}»: las colecciones no deben escribir en consola`);
      if (/expect\(\s*(j\.access_token|v\(['"](?:pwd_|superadmin_password|i_token|a_token|b_token|d_token))/.test(codigo)) {
        problemas.push(`una aserción compara un token o contraseña en «${id}»: un fallo mostraría el valor`);
      }
    }
  }
  if (problemas.length) {
    throw new Error(`Colección «${nombre}» inválida:\n  - ${problemas.join('\n  - ')}`);
  }
}

const verificar = process.argv.includes('--verificar');
let desactualizadas = 0;
for (const [archivo, coleccion] of SALIDAS) {
  validar(archivo, coleccion);
  const texto = JSON.stringify(coleccion, null, 2) + '\n';
  const ruta = join(destino, archivo);
  if (verificar) {
    let actual = '';
    try {
      actual = readFileSync(ruta, 'utf8').replace(/\r\n/g, '\n');
    } catch {
      /* no existe */
    }
    if (actual !== texto) {
      console.error(`DESACTUALIZADA: ${archivo} (ejecute «npm run generar»)`);
      desactualizadas += 1;
    }
  } else {
    writeFileSync(ruta, texto);
    console.log(`${archivo}: ${contarPeticiones(coleccion)} peticiones`);
  }
}
if (verificar) {
  if (desactualizadas) process.exit(1);
  console.log('Colecciones al día con sus fuentes.');
}
