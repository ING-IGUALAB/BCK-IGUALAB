#!/usr/bin/env node
/**
 * Ejecutor de las colecciones de integración y E2E (Newman por API, sin pasar secretos por la línea de comandos).
 *
 *   node scripts/ejecutar.mjs --coleccion integracion --entorno qa
 *   node scripts/ejecutar.mjs --coleccion e2e --entorno qa [--carpeta B --carpeta C]
 *
 * Variables obligatorias (solo se informa su NOMBRE si faltan): IGUALAB_API_URL, IGUALAB_E2E_SUPERADMIN_CORREO,
 * IGUALAB_E2E_SUPERADMIN_PASSWORD. Ver README.md. Códigos de salida: 0 ok · 1 pruebas fallidas · 2 configuración ·
 * 3 backend no disponible · 4 guarda (p. ej. cero pruebas, UAT) · 5 secreto hallado en los reportes.
 */
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import newman from 'newman';

import {
  ErrorDeEjecucion, SALIDA, anioActualEnLima, leerArgumentos, nuevaContrasenaSintetica, nuevoIdDeEjecucion, validarConfiguracion,
} from './lib/configuracion.mjs';
import { esperarBackend } from './lib/espera.mjs';
import { prepararMuestras } from './lib/muestras.mjs';
import { resolverSeleccion } from './lib/seleccion.mjs';
import { buscarSecretos, crearRegistro, datosCreados, escribirResumen } from './lib/resultados.mjs';

const aqui = dirname(fileURLToPath(import.meta.url));
const RAIZ = resolve(aqui, '..');

function contarPeticiones(nodo) {
  return nodo.request ? 1 : (nodo.item ?? []).reduce((suma, hijo) => suma + contarPeticiones(hijo), 0);
}

function ejecutarNewman(opciones) {
  return new Promise((resolver, rechazar) => {
    const emisor = newman.run(opciones, (error, resumen) => (error ? rechazar(error) : resolver(resumen)));
    opciones.alEmitir(emisor);
  });
}

async function principal() {
  const argumentos = leerArgumentos(process.argv.slice(2));
  const configuracion = validarConfiguracion(argumentos);
  const resultados = resolve(configuracion.resultados ?? join(process.cwd(), 'resultados'), configuracion.coleccion);
  const { registrar } = crearRegistro(resultados);

  const idEjecucion = nuevoIdDeEjecucion();
  registrar(`Colección: ${configuracion.coleccion} · entorno: ${configuracion.entorno} · backend: ${configuracion.host} · ejecución: ${idEjecucion}`);
  registrar(`Credenciales: SuperAdmin ${configuracion.superadminCorreo} (contraseña no mostrada).`);

  const coleccion = JSON.parse(readFileSync(join(RAIZ, 'colecciones', configuracion.archivoColeccion), 'utf8'));
  // Requisitos y cierre de la selección: se resuelven y validan ANTES de la primera petición (ver scripts/lib/seleccion.mjs).
  const { carpetas, agregadas } = resolverSeleccion(coleccion, configuracion.carpetas);
  const esperadas = carpetas.length
    ? coleccion.item.filter((n) => carpetas.includes(n.name)).reduce((suma, n) => suma + contarPeticiones(n), 0)
    : contarPeticiones(coleccion);
  if (esperadas === 0) throw new ErrorDeEjecucion('La selección no contiene ninguna petición.', SALIDA.GUARDA);
  registrar(`Peticiones a ejecutar: ${esperadas}${carpetas.length ? ` (carpetas, en orden de ejecución: ${carpetas.join(' | ')})` : ' (colección completa)'}`);
  if (agregadas.length) registrar(`Cierre añadido automáticamente al final de la selección: ${agregadas.join(' | ')}`);

  await esperarBackend({ baseUrl: configuracion.baseUrl, esperaSegundos: configuracion.esperaSegundos, registrar });

  const trabajo = mkdtempSync(join(tmpdir(), 'igualab-pruebas-'));
  const muestras = await prepararMuestras({ trabajo, idEjecucion });
  const anio = anioActualEnLima();
  const contrasenas = Object.fromEntries(['int', 'int_nueva', 'a', 'b', 'd'].map((n) => [`pwd_${n}`, nuevaContrasenaSintetica()]));
  const valores = {
    base_url: configuracion.baseUrl,
    run_id: idEjecucion,
    superadmin_correo: configuracion.superadminCorreo,
    superadmin_password: configuracion.superadminPassword,
    anio_actual: String(anio),
    anio_b: String(anio),
    anio_c: String(anio - 1),
    anio_futuro: String(anio + 1),
    ...contrasenas,
    ...muestras,
  };
  const secretos = [configuracion.superadminPassword, ...Object.values(contrasenas)];
  const entorno = {
    name: `igualab-${configuracion.entorno}`,
    values: Object.entries(valores).map(([key, value]) => ({ key, value, enabled: true, type: /pwd|password/.test(key) ? 'secret' : 'default' })),
  };

  if (configuracion.sinEjecutar) {
    registrar('--sin-ejecutar: configuración, disponibilidad del backend y muestras verificadas; no se ejecutó ninguna petición.');
    return SALIDA.OK;
  }

  const peticiones = [];
  let actual = null;
  const inicio = Date.now();
  const resumen = await ejecutarNewman({
    collection: coleccion,
    environment: entorno,
    workingDir: trabajo,
    folder: carpetas.length ? carpetas : undefined,
    reporters: ['junit'],
    reporter: { junit: { export: join(resultados, 'junit.xml') } },
    timeoutRequest: 420_000,
    timeoutScript: 30_000,
    ignoreRedirects: true,
    insecureFileRead: false,
    color: 'off',
    alEmitir(emisor) {
      emisor.on('beforeItem', (_e, args) => {
        actual = { carpeta: args.item.parent()?.name ?? '', nombre: args.item.name, metodo: '', ruta: '', estado_http: null, milisegundos: 0, aserciones: [] };
      });
      emisor.on('request', (error, args) => {
        if (!actual) return;
        actual.metodo = args.request.method;
        actual.ruta = args.request.url.getPath();
        if (!error && args.response) {
          actual.estado_http = args.response.code;
          actual.milisegundos = args.response.responseTime;
        } else {
          actual.error_de_red = error?.code ?? error?.name ?? 'ERROR';
        }
      });
      emisor.on('assertion', (error, args) => {
        if (!actual) return;
        actual.aserciones.push({ prueba: args.assertion, ok: !error, mensaje: error ? String(error.message ?? '').slice(0, 300) : '' });
      });
      emisor.on('item', () => {
        if (!actual) return;
        const fallidas = actual.aserciones.filter((a) => !a.ok);
        peticiones.push({
          carpeta: actual.carpeta, nombre: actual.nombre, metodo: actual.metodo, ruta: actual.ruta, estado_http: actual.estado_http,
          milisegundos: actual.milisegundos, aserciones_total: actual.aserciones.length, aserciones_fallidas: fallidas.length,
          ...(actual.error_de_red ? { error_de_red: actual.error_de_red } : {}),
        });
        const marca = fallidas.length === 0 && actual.estado_http !== null ? 'OK   ' : 'FALLA';
        registrar(`${marca} ${actual.metodo.padEnd(6)} ${actual.ruta} → ${actual.estado_http ?? actual.error_de_red ?? 'sin respuesta'} (${actual.milisegundos} ms) · ${actual.nombre}`);
        for (const a of fallidas) registrar(`        ✗ ${a.prueba}: ${a.mensaje}`);
        actual = null;
      });
    },
  });

  // ---- Veredicto: nunca verde con cero pruebas, con peticiones omitidas o con fallos --------------------------------------
  const stats = resumen.run.stats;
  const fallos = resumen.run.failures.map((f) => ({
    peticion: f.source?.name ?? '(desconocida)', prueba: f.error?.test ?? f.error?.name ?? 'error', mensaje: String(f.error?.message ?? '').slice(0, 300),
  }));
  const estadisticas = {
    peticiones_esperadas: esperadas,
    peticiones_ejecutadas: peticiones.length,
    aserciones_total: stats.assertions.total,
    aserciones_fallidas: stats.assertions.failed,
    duracion_ms: Date.now() - inicio,
  };
  const problemas = [];
  if (estadisticas.peticiones_ejecutadas !== esperadas) problemas.push(`se ejecutaron ${estadisticas.peticiones_ejecutadas} de ${esperadas} peticiones (¿la corrida se detuvo antes de tiempo?)`);
  if (stats.assertions.total === 0) problemas.push('cero aserciones ejecutadas');
  if (stats.assertions.total < esperadas) problemas.push(`hay menos aserciones (${stats.assertions.total}) que peticiones (${esperadas})`);
  if (peticiones.some((p) => p.estado_http === null)) problemas.push('alguna petición no obtuvo respuesta HTTP');

  const hayFallos = fallos.length > 0 || stats.assertions.failed > 0;
  const veredicto = hayFallos ? 'FALLIDO' : problemas.length ? 'INVÁLIDO' : 'APROBADO';
  // Los scripts reflejan en el ENTORNO solo variables no secretas (IDs y correos sintéticos); los tokens viven solo como variables de colección.
  const variablesFinales = resumen.environment?.values?.members ?? [];
  const tokens = [];
  escribirResumen({
    directorio: resultados,
    contexto: { coleccion: configuracion.coleccion, entorno: configuracion.entorno, host: configuracion.host, run_id: idEjecucion, carpetas: carpetas.length ? carpetas : 'todas' },
    peticiones, estadisticas, fallos, datos: datosCreados(variablesFinales), veredicto,
  });

  registrar('');
  registrar(`Resultado: ${veredicto} · peticiones ${estadisticas.peticiones_ejecutadas}/${esperadas} · aserciones ${stats.assertions.total - stats.assertions.failed}/${stats.assertions.total} · ${(estadisticas.duracion_ms / 1000).toFixed(1)} s`);
  for (const problema of problemas) registrar(`GUARDA: ${problema}`);

  const hallazgos = buscarSecretos(resultados, [...secretos, ...tokens]);
  if (hallazgos.length) {
    registrar(`FUGA DE SECRETOS: se encontró un secreto en ${hallazgos.map((h) => h.slice(resultados.length + 1)).join(', ')} (valor no mostrado). Descarte esos reportes.`);
    return SALIDA.FUGA_DE_SECRETOS;
  }
  if (hayFallos) return SALIDA.PRUEBAS_FALLIDAS;
  return problemas.length ? SALIDA.GUARDA : SALIDA.OK;
}

principal().then(
  (codigo) => process.exit(codigo),
  (error) => {
    if (error instanceof ErrorDeEjecucion) {
      console.error(`ERROR: ${error.message}`);
      process.exit(error.codigoSalida);
    }
    console.error(`ERROR inesperado del ejecutor (${error?.name ?? 'Error'}): ${String(error?.message ?? error).slice(0, 500)}`);
    process.exit(SALIDA.CONFIGURACION);
  },
);
