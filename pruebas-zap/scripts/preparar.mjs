#!/usr/bin/env node
/**
 * Preparación del ZAP API Scan (se ejecuta en un contenedor Node dentro de proxy_net, ANTES de ZAP):
 *   1. valida la configuración y que el destino sea QA;
 *   2. espera al backend (plazo acotado) y descarga /openapi.json (accesible, JSON válido, sin destinos de otros ambientes);
 *   3. clasifica cada operación (alcance/operaciones.json) y deriva la especificación filtrada;
 *   4. autentica al SuperAdmin de QA (UN solo intento), crea un Administrador y una empresa SINTÉTICOS y demuestra que las
 *      sesiones acceden de verdad a endpoints protegidos;
 *   5. escribe la entrada de ZAP (trabajo/) y las credenciales efímeras (privado/, nunca se archivan).
 *
 *   node scripts/preparar.mjs --trabajo DIR --privado DIR --id ID
 *
 * Variables: IGUALAB_API_URL, IGUALAB_E2E_SUPERADMIN_CORREO, IGUALAB_E2E_SUPERADMIN_PASSWORD, IGUALAB_PRUEBAS_ENTORNO=qa.
 * No imprime contraseñas, tokens ni cuerpos de respuesta. Códigos de salida: ver lib/codigos.mjs.
 */
import { chmodSync, copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { clasificar, configuracionDelHook, validarAlcance } from './lib/alcance.mjs';
import { ErrorDeEjecucion, SALIDA } from './lib/codigos.mjs';
import {
  anioActualEnLima, leerArgumentos, nuevaContrasenaSintetica, validarConfiguracion,
} from './lib/configuracion.mjs';
import { esperarBackend } from './lib/espera.mjs';
import { describirFallo, pedir } from './lib/http.mjs';
import { especificacionFiltrada, problemasDeDestino, validarEstructura } from './lib/openapi.mjs';

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const MAX_BYTES_OPENAPI = 8 * 1024 * 1024;
const registrar = (texto) => console.log(texto);

function escribir(ruta, contenido, modo) {
  writeFileSync(ruta, typeof contenido === 'string' ? contenido : `${JSON.stringify(contenido, null, 2)}\n`, { mode: modo });
  chmodSync(ruta, modo);
}

async function descargarOpenapi(baseUrl) {
  let respuesta;
  try {
    respuesta = await pedir(baseUrl, 'GET', '/openapi.json', { plazoMs: 30_000 });
  } catch (error) {
    throw new ErrorDeEjecucion(`GET /openapi.json no respondió (${describirFallo(error)}).`, SALIDA.BACKEND_NO_DISPONIBLE);
  }
  if (respuesta.estado !== 200) throw new ErrorDeEjecucion(`GET /openapi.json devolvió HTTP ${respuesta.estado}.`, SALIDA.BACKEND_NO_DISPONIBLE);
  if (!/json/i.test(respuesta.tipo)) throw new ErrorDeEjecucion(`GET /openapi.json no es JSON (Content-Type: ${respuesta.tipo || 'ausente'}).`, SALIDA.CONFIGURACION);
  if (respuesta.texto.length > MAX_BYTES_OPENAPI) throw new ErrorDeEjecucion('GET /openapi.json supera el tamaño máximo esperado.', SALIDA.CONFIGURACION);
  if (respuesta.cuerpo === null) throw new ErrorDeEjecucion('GET /openapi.json no contiene JSON válido.', SALIDA.CONFIGURACION);
  return respuesta.cuerpo;
}

/** Verifica por qué rol se entra de verdad: respuestas 2xx de endpoints protegidos y los rechazos esperados. */
async function demostrarAcceso({ baseUrl, tokenSuper, tokenAdmin, empresaId, anio }) {
  const comprobaciones = [];
  const comprobar = async (nombre, esperado, metodo, ruta, token, obligatoria = true) => {
    const { estado } = await pedir(baseUrl, metodo, ruta, { token });
    const ok = Array.isArray(esperado) ? esperado.includes(estado) : estado === esperado;
    comprobaciones.push({ nombre, metodo, ruta, esperado, estado, ok, obligatoria });
    registrar(`${ok ? 'OK   ' : 'FALLA'} ${metodo} ${ruta} (${nombre}) → HTTP ${estado}`);
    return ok;
  };
  await comprobar('SuperAdmin lista usuarios', 200, 'GET', '/usuarios', tokenSuper);
  await comprobar('SuperAdmin consulta auditoría', 200, 'GET', '/auditoria', tokenSuper);
  await comprobar('SuperAdmin lee la empresa sintética', 200, 'GET', `/empresas/${empresaId}`, tokenSuper);
  await comprobar('Administrador lista reportes', 200, 'GET', '/reportes', tokenAdmin);
  // El contrato real solo deja al Administrador listar las empresas ACTIVAS (sin el filtro responde 403).
  await comprobar('Administrador lista empresas activas', 200, 'GET', '/empresas?solo_activas=true', tokenAdmin);
  await comprobar('Administrador sin solo_activas recibe 403 (contrato real)', 403, 'GET', '/empresas', tokenAdmin);
  await comprobar('Administrador NO accede a /usuarios', 403, 'GET', '/usuarios', tokenAdmin);
  await comprobar('Sin sesión no accede a /usuarios', 401, 'GET', '/usuarios', undefined);
  await comprobar('Administrador consulta el análisis de la empresa sintética', [200, 404, 409, 503], 'GET', `/analisis-gri/empresas/${empresaId}/anios/${anio}`, tokenAdmin, false);
  const fallidas = comprobaciones.filter((c) => c.obligatoria && !c.ok);
  if (fallidas.length) {
    throw new ErrorDeEjecucion(
      `No se demostró el acceso autenticado esperado (${fallidas.map((c) => `${c.metodo} ${c.ruta} → ${c.estado}`).join('; ')}).`,
      SALIDA.EVIDENCIA,
    );
  }
  return comprobaciones;
}

async function principal() {
  const argumentos = leerArgumentos(process.argv.slice(2), ['trabajo', 'privado', 'id']);
  const trabajo = resolve(argumentos.trabajo);
  const privado = resolve(argumentos.privado);
  const idEjecucion = argumentos.id;
  mkdirSync(trabajo, { recursive: true });
  mkdirSync(privado, { recursive: true });

  const configuracion = validarConfiguracion();
  const { baseUrl } = configuracion;
  registrar(`Preparación ZAP · ejecución ${idEjecucion} · destino ${configuracion.destino.host} (${configuracion.destino.porNombre ? 'identificado como QA por nombre' : 'confirmado explícitamente'})`);

  const alcance = validarAlcance(JSON.parse(readFileSync(join(RAIZ, 'alcance', 'operaciones.json'), 'utf8')));

  await esperarBackend({ baseUrl, esperaSegundos: configuracion.esperaSegundos, registrar });
  const original = await descargarOpenapi(baseUrl);
  validarEstructura(original);
  const problemas = problemasDeDestino(original, baseUrl);
  if (problemas.length) {
    throw new ErrorDeEjecucion(`El OpenAPI contiene destinos distintos del destino de QA; se rechaza la importación:\n  - ${problemas.slice(0, 10).join('\n  - ')}`, SALIDA.CONFIGURACION);
  }
  registrar('OpenAPI real: accesible, válido y sin servidores ni referencias hacia otros ambientes.');

  const { incluidas, excluidas, sinClasificar, ausentes } = clasificar(original, alcance);
  if (sinClasificar.length) {
    throw new ErrorDeEjecucion(
      `Operaciones del OpenAPI sin clasificar en alcance/operaciones.json (decida si se incluyen o se excluyen):\n  - ${sinClasificar.map((o) => `${o.metodo} ${o.ruta}`).join('\n  - ')}`,
      SALIDA.CONFIGURACION,
    );
  }
  if (!incluidas.length) throw new ErrorDeEjecucion('Ninguna operación quedó incluida en el alcance.', SALIDA.CONFIGURACION);
  registrar(`Alcance: ${incluidas.length} operación(es) incluida(s) y ${excluidas.length} excluida(s) de ${incluidas.length + excluidas.length}.`);
  if (ausentes.length) registrar(`Aviso: ${ausentes.length} operación(es) clasificada(s) ya no existen en el OpenAPI: ${ausentes.map((o) => `${o.metodo} ${o.ruta}`).join(', ')}`);

  // ---- Autenticación y datos sintéticos (estado incremental: la limpieza funciona aunque esto falle a medias) ----
  const anio = anioActualEnLima();
  const estado = { id_ejecucion: idEjecucion, usuario_id: null, empresa_id: null, token_superadmin: null, token_administrador: null, secretos: [] };
  const guardarEstado = () => escribir(join(privado, 'estado.json'), estado, 0o600);
  guardarEstado();

  const login = await pedir(baseUrl, 'POST', '/auth/login', { json: { correo: configuracion.superadminCorreo, password: configuracion.superadminPassword } });
  if (login.estado !== 200 || !login.cuerpo?.access_token) {
    const detalle = login.estado === 401 || login.estado === 423 ? ' La cuenta no se reintenta para no bloquearla (5 intentos): revise la credencial IGUALAB_QA_SUPERADMIN.' : '';
    throw new ErrorDeEjecucion(`El inicio de sesión del SuperAdmin de QA falló (HTTP ${login.estado}).${detalle}`, SALIDA.CONFIGURACION);
  }
  if (login.cuerpo.rol !== 'superadmin') throw new ErrorDeEjecucion('La credencial IGUALAB_QA_SUPERADMIN no corresponde a un SuperAdmin.', SALIDA.CONFIGURACION);
  estado.token_superadmin = login.cuerpo.access_token;
  estado.secretos.push(estado.token_superadmin); // la contraseña del SuperAdmin no se persiste: el finalizador la recibe por entorno
  guardarEstado();
  registrar('SuperAdmin de QA autenticado (una sola sesión nueva; la cuenta no será destino de ninguna mutación).');

  const passwordAdmin = nuevaContrasenaSintetica();
  estado.secretos.push(passwordAdmin);
  const correoAdmin = `zap-${idEjecucion}@igualab-zap.example.com`;
  const alta = await pedir(baseUrl, 'POST', '/usuarios', { token: estado.token_superadmin, json: { nombre: `ZAP ${idEjecucion}`, correo: correoAdmin, password: passwordAdmin } });
  if (alta.estado !== 201 || !alta.cuerpo?.id) throw new ErrorDeEjecucion(`No se pudo crear el Administrador sintético (HTTP ${alta.estado}).`, SALIDA.CONFIGURACION);
  if (alta.cuerpo.rol !== 'administrador') throw new ErrorDeEjecucion('La cuenta sintética no quedó con el rol administrador; se aborta.', SALIDA.CONFIGURACION);
  estado.usuario_id = alta.cuerpo.id;
  estado.correo_administrador = correoAdmin;
  guardarEstado();

  const loginAdmin = await pedir(baseUrl, 'POST', '/auth/login', { json: { correo: correoAdmin, password: passwordAdmin } });
  if (loginAdmin.estado !== 200 || !loginAdmin.cuerpo?.access_token) throw new ErrorDeEjecucion(`El Administrador sintético no pudo iniciar sesión (HTTP ${loginAdmin.estado}).`, SALIDA.CONFIGURACION);
  if (loginAdmin.cuerpo.rol !== 'administrador') throw new ErrorDeEjecucion('La sesión sintética no tiene el rol administrador.', SALIDA.CONFIGURACION);
  estado.token_administrador = loginAdmin.cuerpo.access_token;
  estado.secretos.push(estado.token_administrador);
  guardarEstado();

  const empresa = await pedir(baseUrl, 'POST', '/empresas', { token: estado.token_superadmin, json: { nombre: `ZAP ${idEjecucion} Empresa`, sector: 'ENERGIA' } });
  if (empresa.estado !== 201 || !empresa.cuerpo?.id) throw new ErrorDeEjecucion(`No se pudo crear la empresa sintética (HTTP ${empresa.estado}).`, SALIDA.CONFIGURACION);
  estado.empresa_id = empresa.cuerpo.id;
  guardarEstado();
  registrar('Cuenta Administrador y empresa SINTÉTICAS creadas (se deshabilitan/desactivan al terminar).');

  const comprobaciones = await demostrarAcceso({ baseUrl, tokenSuper: estado.token_superadmin, tokenAdmin: estado.token_administrador, empresaId: estado.empresa_id, anio });

  // ---- Entrada de ZAP ----
  const filtrada = especificacionFiltrada(original, { baseUrl, incluidas, empresaId: estado.empresa_id, anio });
  escribir(join(trabajo, 'openapi-filtrado.json'), filtrada, 0o644);
  escribir(join(trabajo, 'hook-config.json'), configuracionDelHook({ baseUrl, incluidas, excluidas }), 0o644);
  copyFileSync(join(RAIZ, 'reglas', 'zap-api-scan.reglas.conf'), join(trabajo, 'zap-api-scan.reglas.conf'));
  copyFileSync(join(RAIZ, 'hooks', 'zap_hooks.py'), join(trabajo, 'zap_hooks.py'));
  // ZAP corre con otro usuario del contenedor: solo este archivo (tokens efímeros de sesiones que se revocan al terminar) es legible por él.
  escribir(join(privado, 'credenciales.json'), { superadmin: estado.token_superadmin, administrador: estado.token_administrador }, 0o644);
  escribir(join(trabajo, 'preparacion.json'), {
    id_ejecucion: idEjecucion,
    destino: { url_base: baseUrl, host: configuracion.destino.host, verificacion: configuracion.destino.porNombre ? 'nombre del host identifica a QA' : 'host confirmado con ZAP_CONFIRMO_DESTINO_QA' },
    openapi: { operaciones: incluidas.length + excluidas.length, servidores_filtrado: filtrada.servers, version: original.openapi },
    incluidas, excluidas, ausentes,
    sinteticos: { administrador: correoAdmin, empresa: `ZAP ${idEjecucion} Empresa` },
    comprobaciones_de_acceso: comprobaciones,
  }, 0o644);
  registrar('Preparación completa: especificación filtrada, reglas, hook y credenciales efímeras listos.');
}

principal().then(
  () => process.exit(SALIDA.OK),
  (error) => {
    if (error instanceof ErrorDeEjecucion) {
      console.error(`ERROR: ${error.message}`);
      process.exit(error.codigoSalida);
    }
    console.error(`ERROR inesperado: ${error?.name ?? 'Error'} (${describirFallo(error)})`);
    process.exit(SALIDA.ERROR_ZAP);
  },
);
