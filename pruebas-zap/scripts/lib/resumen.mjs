/**
 * Resumen legible (Markdown) del alcance y del resultado de la ejecución. No incluye secretos: solo nombres, rutas, conteos y estados.
 */
const celda = (valor) => String(valor ?? '').replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');

export function resumenMarkdown(r) {
  const l = [];
  l.push('# OWASP ZAP · ZAP API Scan del backend de Igualab', '');
  l.push(`**Veredicto:** ${r.veredicto} (código de salida ${r.codigo_salida})  `);
  l.push(`**Motivo:** ${r.motivo}`, '');
  l.push('> Este resultado describe SOLO lo escaneado en el destino interno HTTP de QA. No demuestra que el backend sea seguro,');
  l.push('> no equivale a cobertura completa (ver «Limitaciones») y no sustituye la revisión de los hallazgos.', '');
  l.push('## Ejecución', '');
  l.push(`- Identificador: \`${r.id_ejecucion}\``);
  l.push(`- Destino: ${r.destino.url_base} (verificación: ${r.destino.verificacion})`);
  l.push(`- Imagen de ZAP: \`${r.zap.imagen}\``);
  l.push(`- Comando: \`${r.zap.comando}\``);
  l.push(`- Código del script de ZAP: ${r.zap.codigo ?? 'no ejecutado'} · tiempo agotado: ${r.zap.tiempo_agotado ? 'SÍ' : 'no'} · límite global: ${r.zap.limite_minutos} min`);
  const a = r.hook?.analisis_activo;
  l.push(`- Análisis activo: ${a ? (a.ejecutado ? (a.completo === false ? 'ejecutado PARCIALMENTE' : 'ejecutado') : 'NO demostrado') : 'sin datos del hook'}${a?.peticiones_de_ataque != null ? ` · peticiones de ataque: ${a.peticiones_de_ataque}` : ''}`, '');

  l.push('## Alcance', '');
  l.push(`Operaciones del OpenAPI real: ${r.alcance.total} · incluidas: ${r.alcance.incluidas.length} · excluidas: ${r.alcance.excluidas.length}`, '');
  l.push('### Incluidas', '', '| Método | Ruta | Rol | Motivo |', '|---|---|---|---|');
  for (const o of r.alcance.incluidas) l.push(`| ${o.metodo} | \`${celda(o.ruta)}\` | ${o.rol} | ${celda(o.motivo)} |`);
  l.push('', '### Excluidas', '', '| Método | Ruta | Categoría | Motivo |', '|---|---|---|---|');
  for (const o of r.alcance.excluidas) l.push(`| ${o.metodo} | \`${celda(o.ruta)}\` | ${o.categoria} | ${celda(o.motivo)} |`);
  l.push('');

  if (r.hook) {
    const e5 = r.hook.errores_5xx ?? [];
    l.push('## Errores 5xx (diagnóstico)', '');
    if (!e5.length) {
      l.push('Ninguna respuesta 5xx en lo escaneado.', '');
    } else {
      l.push(`Respuestas 5xx: ${r.hook.errores_5xx_total ?? e5.length} (se listan hasta ${e5.length}). Busque cada \`request_id\` en el log del backend (líneas «Error inesperado» o «Error de base de datos») para ver la excepción.`);
      l.push('El reporte NO conserva el cuerpo, el ataque ni las cabeceras: solo propiedades de la entrada.', '');
      l.push('| Método | Ruta | HTTP | Operación | request_id | Código de error | Cuerpo (caracteres) | NUL | Controles | Sustituto suelto |', '|---|---|---|---|---|---|---|---|---|---|');
      const si = (v) => (v ? 'sí' : 'no');
      for (const e of e5) {
        l.push(`| ${e.metodo} | \`${celda(e.ruta)}\` | ${e.estado_http} | ${celda(e.operacion ?? '(fuera del alcance)')} | ${celda(e.request_id ?? 'no disponible')} | ${celda(e.codigo_de_error ?? '-')} | ${e.tamano_cuerpo} | ${si(e.cuerpo_con_nul)} | ${si(e.cuerpo_con_caracteres_de_control)} | ${si(e.cuerpo_con_sustituto_suelto)} |`);
      }
      l.push('');
    }
  }

  l.push('## Cobertura autenticada', '');
  const c = r.hook?.cobertura_autenticada;
  if (!c) l.push('Sin datos del hook: **no demostrada**.', '');
  else {
    l.push(`Demostrada: **${c.demostrada ? 'sí' : 'NO'}** (se exige al menos una respuesta autenticada distinta de 401/403 por cada rol).`, '');
    l.push('| Rol | Peticiones autenticadas | Con respuesta de la lógica de negocio (no 401/403) | Con 2xx |', '|---|---|---|---|');
    for (const [rol, d] of Object.entries(c.por_rol ?? {})) l.push(`| ${rol} | ${d.autenticadas} | ${d.alcanzaron_la_logica} | ${d.exitosas_2xx} |`);
    if (c.operaciones_sin_cobertura_autenticada?.length) {
      l.push('', 'Operaciones incluidas sin NINGUNA respuesta autenticada distinta de 401/403 (su cobertura autenticada no está demostrada):', '');
      for (const o of c.operaciones_sin_cobertura_autenticada) l.push(`- ${o}`);
    }
    l.push('');
  }

  l.push('## Datos sintéticos y limpieza', '');
  l.push(`- Cuenta Administrador sintética: ${r.sinteticos.administrador ?? 'no creada'}`);
  l.push(`- Empresa sintética: ${r.sinteticos.empresa ?? 'no creada'}`);
  for (const paso of r.limpieza.pasos) l.push(`- ${paso.paso}: ${paso.ok ? 'OK' : 'FALLÓ'}${paso.detalle ? ` (${paso.detalle})` : ''}`);
  l.push('');

  l.push('## Reportes y secretos', '');
  for (const f of r.reportes.archivos) l.push(`- ${f.nombre}: ${f.estado}${f.sustituciones != null ? ` · secretos redactados: ${f.sustituciones}` : ''}`);
  l.push('');

  l.push('## Limitaciones (obligatorio leer)', '');
  for (const t of r.limitaciones) l.push(`- ${t}`);
  l.push('');
  return l.join('\n');
}

export const LIMITACIONES = [
  'PENDIENTE DE ACEPTACIÓN DEL TA: las operaciones que consumen OCI (embeddings), DeepSeek o envían correo están EXCLUIDAS del escaneo automático (incluida la importación). La cobertura NO es completa.',
  'Se excluyen además las operaciones que cambian credenciales, revocan la sesión del escáner o mutan cuentas (incluido el SuperAdmin), y las que crean datos persistentes con valores del escáner. Ver la tabla de exclusiones.',
  'Reglas activas NO ejecutadas (IGNORE) por depender de un servicio OAST externo: 40043 Log4Shell, 40046 SSRF y 40047 Text4Shell. La ausencia de alertas NO descarta esas vulnerabilidades: no se evaluaron. SSRF queda sin cubrir por el escaneo automático.',
  'Regla 40025 (Proxy Disclosure) en IGNORE por indicación expresa del asesor del curso (el proxy Caddy la dispara como Medium): no se evalúa la exposición del proxy y no es una conclusión de seguridad. Es la única excepción de este tipo.',
  'El destino es la URL INTERNA HTTP del contenedor en proxy_net: permite analizar la API, pero NO demuestra la configuración TLS, HSTS ni las cabeceras que añade el proxy público. Las alertas de esas cabeceras deben revisarse contra el endpoint público HTTPS.',
  'Las empresas sin documentos ni análisis validado devuelven errores de negocio (404/409) en las rutas de análisis y reportes: la lógica interna con datos reales solo se ejerce de forma limitada.',
  'La imagen de ZAP está fijada por versión y digest, pero ZAP actualiza sus complementos al arrancar (comportamiento oficial del script); las reglas pasivas/activas pueden variar entre ejecuciones.',
  'Un build verde significa «sin alertas FAIL/WARN en lo escaneado», no «backend seguro».',
];
