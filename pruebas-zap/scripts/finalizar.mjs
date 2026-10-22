#!/usr/bin/env node
/**
 * Cierre del ZAP API Scan (contenedor Node en proxy_net, SIEMPRE se ejecuta, también si ZAP falló o se agotó el tiempo):
 *   1. cierra los datos sintéticos: desactiva la empresa, deshabilita al Administrador (lo que revoca sus sesiones) y cierra la
 *      sesión del escáner del SuperAdmin; comprueba que los tokens quedaron revocados;
 *   2. toma los reportes de ZAP, los valida, REDACTA secretos (JWT, Bearer, contraseñas de la ejecución) y los copia a resultados/;
 *   3. cruza la evidencia del hook (alcance, cobertura autenticada, análisis activo real) y decide el código de salida.
 *
 *   node scripts/finalizar.mjs --trabajo DIR --privado DIR --resultados DIR --id ID --prep-codigo N --zap-codigo N|ninguno
 *        --tiempo-agotado 0|1|2 --limite-minutos N --imagen REF --comando TEXTO
 *
 * Solo lo que está en resultados/ se archiva: trabajo/ y privado/ (tokens, estado) nunca.
 */
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { ErrorDeEjecucion, SALIDA } from './lib/codigos.mjs';
import { leerArgumentos } from './lib/configuracion.mjs';
import { decidirSalida } from './lib/decision.mjs';
import { comprobarEscritura, esErrorDeArchivo, identidad, mensajeDeArchivo } from './lib/diagnostico.mjs';
import { describirFallo, pedir } from './lib/http.mjs';
import { redactar, redactarJson, secretosRemanentes } from './lib/redaccion.mjs';
import { LIMITACIONES, resumenMarkdown } from './lib/resumen.mjs';

const RAIZ = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const registrar = (texto) => console.log(texto);

const leerJson = (ruta) => {
  try {
    return JSON.parse(readFileSync(ruta, 'utf8'));
  } catch {
    return null;
  }
};

/**
 * Lee el estado privado de la preparación distinguiendo «no existe» (la preparación ni llegó a crearlo: no hay nada que cerrar)
 * de «existe pero no se puede leer o está truncado» (puede haber recursos sintéticos que ya no sabemos identificar por su id).
 */
function leerEstado(ruta) {
  let texto;
  try {
    texto = readFileSync(ruta, 'utf8');
  } catch (error) {
    if (error?.code === 'ENOENT') return { estado: null, problema: null };
    return { estado: null, problema: mensajeDeArchivo('cierre', 'leer', ruta, error) };
  }
  try {
    return { estado: JSON.parse(texto), problema: null };
  } catch {
    return { estado: null, problema: `[fase: cierre] ${ruta} existe pero no es JSON válido (la preparación se interrumpió al escribirlo)` };
  }
}

/**
 * Si la preparación quedó a medias, localiza lo que SABEMOS que habría creado por su nombre determinista (correo y empresa de esta
 * ejecución), para poder cerrarlo. Solo se usa si hay credencial de QA y un destino ya verificado por la preparación.
 */
async function recuperarPorNombre({ baseUrl, correo, password, token, id, registrarPaso }) {
  let tokenSuper = token;
  if (!tokenSuper) {
    if (!baseUrl || !correo || !password) return null;
    const login = await pedir(baseUrl, 'POST', '/auth/login', { json: { correo, password } }).catch(() => ({ estado: 0 }));
    if (login.estado !== 200 || login.cuerpo?.rol !== 'superadmin') {
      registrarPaso('Buscar recursos sintéticos por nombre', false, `login del SuperAdmin HTTP ${login.estado}`);
      return null;
    }
    tokenSuper = login.cuerpo.access_token;
  }
  const usuarios = await pedir(baseUrl, 'GET', '/usuarios', { token: tokenSuper }).catch(() => ({ estado: 0 }));
  const empresas = await pedir(baseUrl, 'GET', '/empresas', { token: tokenSuper }).catch(() => ({ estado: 0 }));
  if (usuarios.estado !== 200 || empresas.estado !== 200) {
    registrarPaso('Buscar recursos sintéticos por nombre', false, `GET /usuarios ${usuarios.estado}, GET /empresas ${empresas.estado}`);
    return null;
  }
  const correoEsperado = `zap-${id}@igualab-zap.example.com`.toLowerCase();
  const usuario = (usuarios.cuerpo ?? []).find((u) => String(u.correo).toLowerCase() === correoEsperado);
  const empresa = (empresas.cuerpo ?? []).find((e) => e.nombre === `ZAP ${id} Empresa`);
  registrarPaso('Buscar recursos sintéticos por nombre', true, `${usuario ? 'cuenta encontrada' : 'sin cuenta'}, ${empresa ? 'empresa encontrada' : 'sin empresa'}`);
  return { token_superadmin: tokenSuper, usuario_id: usuario?.id ?? null, empresa_id: empresa?.id ?? null };
}

// ------------------------------------------------------------------------------------------------------------------ limpieza
async function limpiar({ baseUrl, correo, password, estado: estadoLeido, problemaEstado, id }) {
  const pasos = [];
  // `informativo`: deja constancia de un hecho sin hacer fallar el cierre (p. ej. el estado era ilegible pero se verificó por nombre).
  const paso = (nombre, ok, detalle = '', informativo = false) => {
    pasos.push({ paso: nombre, ok, ...(detalle ? { detalle } : {}), ...(informativo ? { informativo: true } : {}) });
    registrar(`${ok ? 'OK   ' : 'FALLA'} limpieza · ${nombre}${detalle ? ` (${detalle})` : ''}`);
    return ok;
  };
  const resultado = () => ({ ok: pasos.filter((p) => !p.informativo).every((p) => p.ok), pasos });

  let estado = estadoLeido;
  if (problemaEstado) paso('Leer el estado privado de la preparación', false, problemaEstado, true);
  // Preparación incompleta: sin estado legible, o con sesión pero sin ids. Se busca por el nombre determinista de esta ejecución.
  const sinIdentificar = Boolean(problemaEstado) || Boolean(estado?.token_superadmin && (!estado.usuario_id || !estado.empresa_id));
  if (sinIdentificar && baseUrl) {
    const hallado = await recuperarPorNombre({ baseUrl, correo, password, token: estado?.token_superadmin, id, registrarPaso: paso });
    if (hallado) {
      estado = { ...(estado ?? {}), usuario_id: estado?.usuario_id ?? hallado.usuario_id, empresa_id: estado?.empresa_id ?? hallado.empresa_id, token_superadmin: hallado.token_superadmin };
    } else if (problemaEstado) {
      paso('No se pudo comprobar si quedaron recursos sintéticos', false, `revise en QA la cuenta zap-${id}@igualab-zap.example.com y la empresa «ZAP ${id} Empresa»`);
      return resultado();
    }
  } else if (problemaEstado) {
    // Sin destino/credencial no se puede ni buscar por nombre: no se afirma que esté limpio.
    paso('No se pudo comprobar si quedaron recursos sintéticos', false, `sin destino o credencial para buscarlos; revise en QA la cuenta zap-${id}@igualab-zap.example.com y la empresa «ZAP ${id} Empresa»`);
    return resultado();
  }
  const hayRecursos = Boolean(estado?.usuario_id || estado?.empresa_id);
  if (!estado || (!hayRecursos && !estado.token_superadmin)) {
    if (!pasos.length) paso('No se crearon recursos sintéticos ni sesiones que cerrar', true);
    return resultado();
  }
  if (!baseUrl || !estado.token_superadmin) {
    paso('Sesión del SuperAdmin disponible para cerrar los recursos', false, 'sin destino o sin token; los recursos sintéticos pueden seguir activos');
    return resultado();
  }

  let token = estado.token_superadmin;
  const llamar = async (metodo, ruta, t = token) => {
    try {
      return await pedir(baseUrl, metodo, ruta, { token: t });
    } catch (error) {
      return { estado: 0, cuerpo: null, error: describirFallo(error) };
    }
  };

  // Si la sesión del escáner caducó o se revocó, se reabre UNA sola vez con la credencial de QA.
  let sondeo = await llamar('GET', `/empresas/${estado.empresa_id ?? '00000000-0000-4000-8000-000000000000'}`);
  if (sondeo.estado === 401 && correo && password) {
    const relogin = await pedir(baseUrl, 'POST', '/auth/login', { json: { correo, password } }).catch(() => ({ estado: 0 }));
    if (relogin.estado === 200 && relogin.cuerpo?.access_token && relogin.cuerpo.rol === 'superadmin') {
      token = relogin.cuerpo.access_token;
      paso('Sesión del SuperAdmin reabierta para el cierre', true, 'la anterior había caducado');
    } else {
      paso('Sesión del SuperAdmin reabierta para el cierre', false, `login HTTP ${relogin.estado}`);
    }
  }

  if (estado.empresa_id) {
    const r = await llamar('PATCH', `/empresas/${estado.empresa_id}/desactivar`);
    const comprobacion = await llamar('GET', `/empresas/${estado.empresa_id}`);
    paso('Empresa sintética desactivada', comprobacion.estado === 200 && comprobacion.cuerpo?.activa === false, `PATCH ${r.estado}, GET ${comprobacion.estado}`);
  }
  if (estado.usuario_id) {
    const r = await llamar('PATCH', `/usuarios/${estado.usuario_id}/deshabilitar`);
    paso('Administrador sintético deshabilitado', r.estado === 200 && r.cuerpo?.habilitado === false, `PATCH ${r.estado}`);
    if (estado.token_administrador) {
      const revocada = await llamar('GET', '/reportes', estado.token_administrador);
      paso('Sesión del Administrador sintético revocada (401)', revocada.estado === 401, `GET /reportes ${revocada.estado}`);
    }
  }
  const salida = await llamar('POST', '/auth/logout');
  const cerrada = await llamar('GET', '/usuarios');
  paso('Sesión del escáner del SuperAdmin cerrada (401 posterior)', salida.estado === 204 && cerrada.estado === 401, `logout ${salida.estado}, GET /usuarios ${cerrada.estado}`);
  return resultado();
}

// ------------------------------------------------------------------------------------------------------------------ reportes
function nivelesDeLasReglas() {
  const niveles = new Map();
  const texto = readFileSync(join(RAIZ, 'reglas', 'zap-api-scan.reglas.conf'), 'utf8');
  for (const linea of texto.split('\n')) {
    if (linea.startsWith('#') || !linea.trim()) continue;
    const [id, nivel] = linea.split('\t');
    niveles.set(id, nivel);
  }
  return niveles;
}

function validarContenido(nombre, texto) {
  if (!texto.trim()) return 'vacío';
  if (nombre.endsWith('.json')) {
    let datos;
    try {
      datos = JSON.parse(texto);
    } catch {
      return 'JSON inválido';
    }
    if (!datos || !Array.isArray(datos.site)) return 'JSON sin la lista "site" de ZAP';
    return null;
  }
  return /<html[\s>]/i.test(texto) ? null : 'HTML sin etiqueta <html>';
}

function resumirAlertas(textoJson, niveles) {
  let datos;
  try {
    datos = JSON.parse(textoJson);
  } catch {
    return { total: 0, por_riesgo: {}, alertas: [] };
  }
  const alertas = (datos.site ?? []).flatMap((sitio) => sitio.alerts ?? []);
  const porRiesgo = {};
  const lista = alertas.map((a) => {
    const riesgo = String(a.riskdesc ?? '').split(' ')[0] || 'Desconocido';
    porRiesgo[riesgo] = (porRiesgo[riesgo] ?? 0) + 1;
    return { pluginid: String(a.pluginid), nombre: a.name ?? a.alert, riesgo, nivel_regla: niveles.get(String(a.pluginid)) ?? 'WARN', instancias: Number(a.count ?? (a.instances ?? []).length) };
  });
  return { total: alertas.length, por_riesgo: porRiesgo, alertas: lista };
}

/** Copia a resultados/ solo contenido redactado y verificado. Si algo no se puede limpiar, NO se archiva ese archivo. */
function publicar({ origen, destino, secretos, nombre }) {
  if (!existsSync(origen)) return { nombre, estado: 'ausente', archivado: false };
  let texto;
  try {
    texto = readFileSync(origen, 'utf8');
  } catch (error) {
    return { nombre, estado: `NO leído (${error?.code ?? 'ERROR'})`, archivado: false, lectura: false };
  }
  const { texto: limpio, sustituciones } = nombre.endsWith('.json') ? redactarJson(texto, secretos) : redactar(texto, secretos);
  const restos = secretosRemanentes(limpio, secretos);
  if (restos.length) {
    try {
      rmSync(destino, { force: true }); // también cuando origen y destino son el mismo archivo (consola-zap.log): nada con secretos queda en resultados/
    } catch { /* si ni siquiera se puede borrar, el finalizador lo informa por el código de escritura */ }
    return { nombre, estado: `NO archivado: quedan secretos (${restos.join(', ')})`, archivado: false, secretos: false };
  }
  try {
    rmSync(destino, { force: true }); // un archivo previo de otro propietario (p. ej. copiado por `docker cp`) se reemplaza, no se sobrescribe
    writeFileSync(destino, limpio, { mode: 0o644 });
  } catch (error) {
    return { nombre, estado: `NO archivado: no se pudo escribir (${error?.code ?? 'ERROR'})`, archivado: false, escritura: false, secretos: true, contenido: limpio, sustituciones };
  }
  return { nombre, estado: 'archivado', archivado: true, sustituciones, secretos: true, contenido: limpio };
}

async function principal() {
  const a = leerArgumentos(process.argv.slice(2), [
    'trabajo', 'privado', 'resultados', 'id', 'prep-codigo', 'zap-codigo', 'tiempo-agotado', 'limite-minutos', 'imagen', 'comando',
  ]);
  const trabajo = resolve(a.trabajo);
  const privado = resolve(a.privado);
  const resultados = resolve(a.resultados);
  const prepCodigo = Number(a['prep-codigo']);
  const zapEjecutado = a['zap-codigo'] !== 'ninguno';
  const zapCodigo = zapEjecutado ? Number(a['zap-codigo']) : null;
  const tiempoAgotado = a['tiempo-agotado'] === '1';
  const interrumpido = a['tiempo-agotado'] === '2'; // cierre de emergencia tras una cancelación

  // Los directorios de datos los aporta el volumen. Si /resultados no es escribible NO se aborta: se sigue cerrando los datos sintéticos y
  // se informa por consola (con fase, ruta, código y dueño de la carpeta) y con el código ARCHIVOS.
  let problemaResultados = null;
  try {
    comprobarEscritura('cierre', [resultados]);
  } catch (error) {
    problemaResultados = error.message;
    registrar(`ERROR: ${problemaResultados}`);
  }
  registrar(`Cierre: ${identidad()} · ${trabajo}, ${privado}, ${resultados}`);

  // Cierre de emergencia (post de Jenkins) cuando el cierre normal ya completó la limpieza: no hay nada que repetir.
  if (interrumpido && existsSync(join(trabajo, '.cierre-completo'))) {
    registrar('Cierre de emergencia: los datos sintéticos ya estaban cerrados; no se repite nada.');
    return SALIDA.OK;
  }

  const { estado, problema: problemaEstado } = leerEstado(join(privado, 'estado.json'));
  const preparacion = leerJson(join(trabajo, 'preparacion.json'));
  const baseUrl = (process.env.IGUALAB_API_URL ?? '').trim().replace(/\/+$/, '');
  const secretos = [process.env.IGUALAB_E2E_SUPERADMIN_PASSWORD, ...(estado?.secretos ?? [])].filter(Boolean);

  registrar('--- Cierre de datos sintéticos ---');
  const limpieza = await limpiar({
    baseUrl, correo: process.env.IGUALAB_E2E_SUPERADMIN_CORREO, password: process.env.IGUALAB_E2E_SUPERADMIN_PASSWORD, estado, problemaEstado, id: a.id,
  });

  // Marca el cierre de los datos sintéticos como hecho: el `post` del stage (ejecutar-zap.sh limpiar) no lo repite.
  if (limpieza.ok) {
    try {
      writeFileSync(join(trabajo, '.cierre-completo'), '');
    } catch (error) {
      registrar(`Aviso: ${mensajeDeArchivo('cierre', 'escribir', join(trabajo, '.cierre-completo'), error)}`);
    }
  }

  // Reintento desde el `post` cuando el cierre normal ya publicó su resumen: solo se repite la limpieza; el veredicto original se conserva.
  if (interrumpido && existsSync(join(resultados, 'resumen-zap.json'))) {
    registrar('Reintento de limpieza: el resumen y los reportes de la ejecución se conservan sin cambios.');
    return limpieza.ok ? SALIDA.OK : SALIDA.LIMPIEZA;
  }

  registrar('--- Reportes y evidencia ---');
  const niveles = nivelesDeLasReglas();
  const archivos = [];
  let secretosOk = true;
  const piezas = [
    ['backend-zap.html', join(trabajo, 'backend-zap.html')],
    ['backend-zap.json', join(trabajo, 'backend-zap.json')],
    ['openapi-filtrado.json', join(trabajo, 'openapi-filtrado.json')],
    ['cobertura-zap.json', join(trabajo, 'zap-hook-resumen.json')],
    ['consola-zap.log', join(resultados, 'consola-zap.log')], // lo escribe ejecutar-zap.sh (ya filtrado con sed); aquí se verifica
  ];
  let alertas = { total: 0, por_riesgo: {}, alertas: [] };
  const reportesMotivos = [];
  for (const [nombre, origen] of piezas) {
    const salida = publicar({ origen, destino: join(resultados, nombre), secretos, nombre });
    const esReporte = nombre.startsWith('backend-zap');
    if (salida.archivado && esReporte) {
      const problema = validarContenido(nombre, salida.contenido);
      if (problema) {
        salida.estado = `archivado, pero INVÁLIDO: ${problema}`;
        reportesMotivos.push(`${nombre}: ${problema}`);
      } else if (nombre.endsWith('.json')) alertas = resumirAlertas(salida.contenido, niveles);
    }
    if (esReporte && !salida.archivado && salida.secretos !== false) reportesMotivos.push(`${nombre}: ${salida.estado === 'ausente' ? 'no se generó' : salida.estado}`);
    if (salida.secretos === false) secretosOk = false;
    archivos.push({ nombre, estado: salida.estado, sustituciones: salida.sustituciones });
    delete salida.contenido;
  }
  if (!zapEjecutado) reportesMotivos.length = 0; // ZAP no corrió: ya se informa por la preparación
  const reportes = { ok: reportesMotivos.length === 0, motivo: reportesMotivos.length ? `Reportes esperados ausentes o inválidos (${reportesMotivos.join('; ')}).` : undefined };

  let hook = leerJson(join(trabajo, 'zap-hook-resumen.json'));
  if (hook?.error_del_hook) hook = null; // el hook falló al resumir: no hay evidencia
  if (!zapEjecutado) hook = null;

  const decision = decidirSalida({ prepCodigo, zapEjecutado, zapCodigo, tiempoAgotado, interrumpido, reportes, secretos: { ok: secretosOk }, hook, limpieza });

  const incongruencia = zapCodigo === 0 && alertas.alertas.some((x) => x.nivel_regla === 'FAIL')
    ? 'ZAP salió con 0 pero el JSON contiene alertas de nivel FAIL según la configuración: revise el reporte.' : null;

  const resumen = {
    id_ejecucion: a.id,
    veredicto: decision.veredicto,
    codigo_salida: decision.codigo,
    motivo: decision.motivo,
    destino: preparacion?.destino ?? { url_base: baseUrl || '(sin definir)', verificacion: 'preparación no completada' },
    zap: {
      imagen: a.imagen, comando: a.comando, codigo: zapCodigo, tiempo_agotado: tiempoAgotado, limite_minutos: Number(a['limite-minutos']),
      version: '2.17.0',
    },
    alcance: {
      total: (preparacion?.incluidas?.length ?? 0) + (preparacion?.excluidas?.length ?? 0),
      incluidas: preparacion?.incluidas ?? [],
      excluidas: preparacion?.excluidas ?? [],
    },
    hook: hook ? {
      analisis_activo: hook.analisis_activo, cobertura_autenticada: hook.cobertura_autenticada, urls_importadas: hook.urls_importadas,
      total_peticiones: hook.total_peticiones, violaciones_total: hook.violaciones_total,
      autorizacion_incorrecta_total: hook.autorizacion_incorrecta_total ?? 0,
    } : null,
    alertas,
    advertencias: [incongruencia, ...(preparacion?.ausentes?.length ? [`Operaciones clasificadas que ya no existen: ${preparacion.ausentes.length}`] : [])].filter(Boolean),
    sinteticos: preparacion?.sinteticos ?? {},
    limpieza,
    reportes: { archivos },
    limitaciones: LIMITACIONES,
  };

  // Los resúmenes también pasan por la redacción y su verificación.
  const textoJson = JSON.stringify(resumen, null, 2);
  const textoMd = resumenMarkdown(resumen);
  const alcanceJson = JSON.stringify({ id_ejecucion: a.id, incluidas: resumen.alcance.incluidas, excluidas: resumen.alcance.excluidas, pendiente_de_aceptacion_del_ta: resumen.alcance.excluidas.filter((o) => o.categoria === 'servicio_externo').map((o) => `${o.metodo} ${o.ruta}`) }, null, 2);
  const fallosDeEscritura = [];
  let textoMdSeguro = '';
  for (const [nombre, contenido] of [['resumen-zap.json', textoJson], ['resumen-zap.md', textoMd], ['alcance-zap.json', alcanceJson]]) {
    const { texto } = nombre.endsWith('.json') ? redactarJson(contenido, secretos) : redactar(contenido, secretos);
    if (secretosRemanentes(texto, secretos).length) {
      try { rmSync(join(resultados, nombre), { force: true }); } catch { /* se informa abajo si no se puede escribir */ }
      registrar(`ERROR: ${nombre} contenía secretos y no se archivó.`);
      decision.codigo = SALIDA.SECRETOS;
      continue;
    }
    if (nombre === 'resumen-zap.md') textoMdSeguro = texto;
    try {
      rmSync(join(resultados, nombre), { force: true });
      writeFileSync(join(resultados, nombre), texto, { mode: 0o644 });
    } catch (error) {
      fallosDeEscritura.push(mensajeDeArchivo('cierre', 'escribir', join(resultados, nombre), error));
    }
  }
  for (const f of archivos.filter((x) => x.estado.includes('no se pudo escribir'))) {
    fallosDeEscritura.push(`[fase: cierre] escribir ${join(resultados, f.nombre)} falló (${f.estado})`);
  }
  // Sin poder dejar los artefactos, un éxito (0/1/2) no es creíble: el veredicto pasa a error técnico ARCHIVOS y el resumen sale por consola.
  if ((problemaResultados || fallosDeEscritura.length) && [SALIDA.OK, SALIDA.REGLAS_FAIL, SALIDA.ADVERTENCIAS].includes(decision.codigo)) {
    decision.codigo = SALIDA.ARCHIVOS;
    decision.veredicto = 'FALLO TÉCNICO';
    decision.motivo = 'No se pudieron escribir los artefactos en /resultados; el análisis no se considera archivado.';
  }
  if (problemaResultados || fallosDeEscritura.length) {
    for (const fallo of fallosDeEscritura.length ? fallosDeEscritura : [problemaResultados]) registrar(`ERROR: ${fallo}`);
    registrar('--- Resumen (no se pudo escribir en resultados; se muestra aquí) ---');
    registrar(textoMdSeguro || textoMd);
  }

  registrar('--- Resultado ---');
  registrar(`Veredicto: ${decision.veredicto} · código de salida ${decision.codigo}`);
  registrar(`Motivo: ${decision.motivo}`);
  registrar(`Alertas en el reporte: ${alertas.total} ${JSON.stringify(alertas.por_riesgo)}`);
  if (resumen.hook) {
    registrar(`Análisis activo: ejecutado=${resumen.hook.analisis_activo?.ejecutado} completo=${resumen.hook.analisis_activo?.completo} peticiones de ataque=${resumen.hook.analisis_activo?.peticiones_de_ataque}`);
    registrar(`Cobertura autenticada demostrada: ${resumen.hook.cobertura_autenticada?.demostrada}`);
  }
  for (const texto of resumen.advertencias) registrar(`Aviso: ${texto}`);
  registrar('RECORDATORIO: la exclusión de OCI/DeepSeek/correo está pendiente de aceptación del TA; este resultado no equivale a cobertura completa ni a «backend seguro».');
  return decision.codigo;
}

principal().then(
  (codigo) => process.exit(codigo),
  (error) => {
    if (error instanceof ErrorDeEjecucion) {
      console.error(`ERROR: ${error.message}`);
      process.exit(error.codigoSalida);
    }
    if (esErrorDeArchivo(error)) {
      console.error(`ERROR: ${mensajeDeArchivo('cierre', error.syscall, error.path ?? '(sin ruta)', error)}`);
      process.exit(SALIDA.ARCHIVOS);
    }
    console.error(`ERROR inesperado en el cierre: ${error?.name ?? 'Error'}`);
    process.exit(SALIDA.ERROR_ZAP);
  },
);
