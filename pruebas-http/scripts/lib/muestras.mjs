/**
 * Muestras sintéticas por ejecución. El contenido varía con el identificador de ejecución (SHA-256 distinto en cada corrida,
 * sin duplicados accidentales) pero produce SIEMPRE las mismas identidades GRI sin ambigüedad de edición y sin sanciones
 * (el flujo B no depende del detector de sanciones).
 *
 *   muestras/b-reporte.md   documento del flujo B
 *   muestras/c-copia.md     MISMOS bytes que b-reporte.md con otro nombre (flujo C: duplicado por contenido)
 *   muestras/d-reporte.md   documento válido y exclusivo del flujo D (bytes distintos de B/C)
 *   muestras/estaticas/*    archivos fijos para los rechazos de la colección de integración
 */
import { createHash } from 'node:crypto';
import { cp, mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { SALIDA, ErrorDeEjecucion } from './configuracion.mjs';

const aqui = dirname(fileURLToPath(import.meta.url));
const RAIZ = join(aqui, '..', '..');
export const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

export async function prepararMuestras({ trabajo, idEjecucion }) {
  const plantilla = (await readFile(join(RAIZ, 'muestras', 'plantilla-reporte-gri.md'), 'utf8')).replace(/\r\n/g, '\n');
  const generar = (flujo) => plantilla.replaceAll('{{RUN_ID}}', idEjecucion).replaceAll('{{FLUJO}}', flujo);
  const carpeta = join(trabajo, 'muestras');
  await mkdir(carpeta, { recursive: true });

  const textoB = generar('B y C');
  const textoD = generar('D');
  const bytesB = Buffer.from(textoB, 'utf8');
  await writeFile(join(carpeta, 'b-reporte.md'), bytesB);
  await writeFile(join(carpeta, 'c-copia.md'), bytesB);
  await writeFile(join(carpeta, 'd-reporte.md'), Buffer.from(textoD, 'utf8'));
  await cp(join(RAIZ, 'muestras', 'estaticas'), join(carpeta, 'estaticas'), { recursive: true });

  // Comprobaciones previas: el ejecutor no debe depender de que el servidor detecte un error de preparación.
  const hashB = sha256(await readFile(join(carpeta, 'b-reporte.md')));
  const hashC = sha256(await readFile(join(carpeta, 'c-copia.md')));
  const hashD = sha256(await readFile(join(carpeta, 'd-reporte.md')));
  if (hashB !== hashC) throw new ErrorDeEjecucion('Las muestras de B y C deben tener exactamente los mismos bytes.', SALIDA.GUARDA);
  if (hashD === hashB) throw new ErrorDeEjecucion('La muestra de D debe ser distinta de la de B/C.', SALIDA.GUARDA);
  if (/[^\u0000-￿]/u.test(textoB)) throw new ErrorDeEjecucion('La muestra no debe contener caracteres fuera del plano básico (los offsets se comparan en UTF-16).', SALIDA.GUARDA);
  if (Buffer.byteLength(textoB) > 20_000) throw new ErrorDeEjecucion('La muestra debe ser mínima (OCI cobra por cada fragmento).', SALIDA.GUARDA);

  return {
    sha256_b: hashB,
    tamano_b: String(bytesB.length),
    nombre_b: 'b-reporte.md',
    nombre_c: 'c-copia.md',
    nombre_d: 'd-reporte.md',
    texto_b: textoB,
  };
}
