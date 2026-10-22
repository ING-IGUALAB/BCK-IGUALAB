/**
 * Proceso que SIMULA el contenedor de ZAP según FALSO_ZAP_ESCENARIO. No ejecuta ZAP: escribe reportes/resumen de ejemplo y termina
 * con el código indicado para verificar cómo el ejecutor interpreta los desenlaces. Los reportes incluyen a propósito un JWT y la
 * cabecera Authorization como evidencia, para comprobar que se redactan.
 */
import { appendFileSync, existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const [nombre, trabajo, privado, estado] = process.argv.slice(2);
const escenario = process.env.FALSO_ZAP_ESCENARIO ?? 'exito';
const log = (t) => appendFileSync(join(estado, `log-${nombre}.txt`), `${t}\n`);
const terminar = (codigo) => { writeFileSync(join(estado, `salida-${nombre}`), String(codigo)); process.exit(codigo); };

const credenciales = existsSync(join(privado, 'credenciales.json')) ? JSON.parse(readFileSync(join(privado, 'credenciales.json'), 'utf8')) : {};
const jwtDeLaEjecucion = credenciales.administrador ?? 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.firmaFalsaDeEjemplo';
const config = JSON.parse(readFileSync(join(trabajo, 'hook-config.json'), 'utf8'));

log('Total of 19 URLs');
log(`Authorization: Bearer ${jwtDeLaEjecucion}`); // debe quedar redactado en la consola
if (escenario === 'colgado') { log('Active Scan progress %: 12'); setInterval(() => {}, 1000); } else {
  const alerta = (pluginid, nombreAlerta, riesgo) => ({
    pluginid, alertRef: pluginid, alert: nombreAlerta, name: nombreAlerta, riskdesc: `${riesgo} (Medium)`, count: '1',
    instances: [{ uri: `${config.destino}/empresas`, method: 'GET', param: 'q', attack: "' OR 1=1--", evidence: `Authorization: Bearer ${jwtDeLaEjecucion}` }],
  });
  const alertas = { exito: [], advertencia: [alerta('10021', 'X-Content-Type-Options Header Missing', 'Low')], 'con-5xx': [alerta('100000', 'A Server Error response code was returned by the server', 'Low')], fallo: [alerta('40018', 'SQL Injection', 'High'), alerta('10021', 'X-Content-Type-Options Header Missing', 'Low')] };
  const sitio = { '@name': config.destino, alerts: alertas[escenario] ?? [] };
  const hayReportes = !['tecnico', 'sin-reportes'].includes(escenario);
  if (hayReportes) {
    writeFileSync(join(trabajo, 'backend-zap.json'), JSON.stringify({ '@programName': 'ZAP', '@version': '2.17.0', site: [sitio] }));
    writeFileSync(join(trabajo, 'backend-zap.html'), `<html><body><h1>ZAP</h1><pre>Authorization: Bearer ${jwtDeLaEjecucion}</pre></body></html>`);
  }
  const buenoRol = { autenticadas: 10, alcanzaron_la_logica: 10, exitosas_2xx: 5 };
  const resumenHook = {
    version: 1, reglas_de_autenticacion: ['administrador', 'superadmin'], urls_importadas: 19, violaciones_total: 0, violaciones_de_alcance: [], urls_fuera_del_destino: [],
    total_peticiones: 4000,
    analisis_activo: { ejecutado: true, completo: true, escaneos: 1, peticiones_de_ataque: 3500 },
    cobertura_autenticada: { demostrada: true, por_rol: { superadmin: buenoRol, administrador: buenoRol }, operaciones_sin_cobertura_autenticada: [] },
  };
  if (escenario === 'con-5xx') {
    // Diagnóstico 5xx tal como lo escribe el hook (sin cuerpos). La ruta lleva a propósito algo con forma de JWT: lo archivado debe redactarlo.
    const jwtEnRuta = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ6YXAifQ.firmaQueNoDebeArchivarse1234';
    Object.assign(resumenHook, {
      errores_5xx_total: 2,
      errores_5xx: [
        { metodo: 'POST', ruta: '/auth/login', operacion: 'POST /auth/login', estado_http: 500, request_id: '3f2b8c1e-5d4a-4e7b-9c0d-1a2b3c4d5e6f', codigo_de_error: 'INTERNAL_ERROR', id_mensaje_zap: '1399', tiene_consulta: false, tipo_de_contenido: 'application/json', tamano_cuerpo: 100050, cuerpo_con_nul: true, cuerpo_con_caracteres_de_control: false, cuerpo_con_sustituto_suelto: false },
        { metodo: 'PATCH', ruta: `/empresas/${jwtEnRuta}`, operacion: null, estado_http: 500, request_id: null, codigo_de_error: null, id_mensaje_zap: '6058', tiene_consulta: true, tipo_de_contenido: 'application/json', tamano_cuerpo: 40, cuerpo_con_nul: false, cuerpo_con_caracteres_de_control: true, cuerpo_con_sustituto_suelto: false },
      ],
    });
  }
  if (escenario === 'alcance') Object.assign(resumenHook, { violaciones_total: 1, violaciones_de_alcance: [{ metodo: 'POST', ruta: '/auth/logout', estado_http: 204 }] });
  if (escenario === 'sin-activo') resumenHook.analisis_activo = { ejecutado: false, completo: null, escaneos: 0, peticiones_de_ataque: 0 };
  if (escenario === 'activo-parcial') resumenHook.analisis_activo.completo = false;
  if (escenario === 'solo-401') resumenHook.cobertura_autenticada = { demostrada: false, por_rol: { superadmin: { autenticadas: 5, alcanzaron_la_logica: 0, exitosas_2xx: 0 }, administrador: buenoRol }, operaciones_sin_cobertura_autenticada: ['GET /usuarios'] };
  if (hayReportes && escenario !== 'sin-hook' && escenario !== 'tecnico') writeFileSync(join(trabajo, 'zap-hook-resumen.json'), JSON.stringify(resumenHook));
  log('FAIL-NEW: 0\tWARN-NEW: 0');
  terminar({ exito: 0, advertencia: 2, 'con-5xx': 2, fallo: 1, tecnico: 3 }[escenario] ?? 0);
}
