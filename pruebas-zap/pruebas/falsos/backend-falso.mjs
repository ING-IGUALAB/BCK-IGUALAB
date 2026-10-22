/**
 * Backend FALSO, solo para verificar el ejecutor en local aislado. No es el sistema desplegado: implementa lo mínimo de la API real
 * (login, cuentas, empresas, revocación de sesiones) con el mismo contrato de estados HTTP. Nada de esto demuestra un escaneo de QA.
 */
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const aqui = dirname(fileURLToPath(import.meta.url));
const OPENAPI = JSON.parse(readFileSync(join(aqui, '..', 'fixtures', 'openapi-real.json'), 'utf8'));

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64url');
const nuevoJwt = (sub, rol) => `${b64({ alg: 'HS256', typ: 'JWT' })}.${b64({ sub, rol, jti: Math.random().toString(16).slice(2), exp: 9999999999 })}.firmaDePrueba${Math.random().toString(36).slice(2, 12)}`;

/** modos: normal | desactivar-falla-una-vez | login401 | sin-health | openapi-servidor-ajeno | operacion-nueva | openapi-ref-remota | sin-openapi | rol-incorrecto */
export function crearBackendFalso({ modo = 'normal', superadmin = { correo: 'oscar@igualab.test', password: 'Clave-SA-1!' } } = {}) {
  const estado = { usuarios: new Map(), empresas: new Map(), sesiones: new Map(), peticiones: [], intentosLogin: 0 };
  const sa = { id: 'sa-0001', correo: superadmin.correo, password: superadmin.password, rol: 'superadmin', habilitado: true };
  estado.usuarios.set(sa.id, sa);

  const servidor = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x');
    const cuerpoTexto = await new Promise((resolver) => { let t = ''; req.on('data', (c) => { t += c; }); req.on('end', () => resolver(t)); });
    let cuerpo = {};
    try { cuerpo = cuerpoTexto ? JSON.parse(cuerpoTexto) : {}; } catch { cuerpo = {}; }
    const token = (req.headers.authorization ?? '').replace(/^Bearer\s+/i, '');
    const sesion = estado.sesiones.get(token);
    const actor = sesion && !sesion.revocada ? estado.usuarios.get(sesion.usuario) : null;
    const usuarioValido = actor && actor.habilitado ? actor : null;
    estado.peticiones.push({ metodo: req.method, ruta: url.pathname, autenticada: Boolean(token), estado: 0 });
    const responder = (codigo, datos) => {
      estado.peticiones.at(-1).estado = codigo;
      res.writeHead(codigo, { 'content-type': 'application/json' });
      res.end(datos === undefined ? '' : JSON.stringify(datos));
    };
    const exigir = (rol) => {
      if (!usuarioValido) { responder(401, { error: { codigo: 'INVALID_SESSION' } }); return false; }
      if (rol && usuarioValido.rol !== rol) { responder(403, { error: { codigo: 'FORBIDDEN' } }); return false; }
      return true;
    };
    const { pathname: p } = url;
    const m = req.method;

    if (p === '/health' && m === 'GET') return modo === 'sin-health' ? responder(503, { status: 'iniciando' }) : responder(200, { status: 'ok' });
    if (p === '/openapi.json' && m === 'GET') {
      if (modo === 'sin-openapi') return responder(404, { detail: 'Not Found' });
      const copia = structuredClone(OPENAPI);
      if (modo === 'openapi-servidor-ajeno') copia.servers = [{ url: 'https://igualab-uat.ejemplo.pe' }];
      if (modo === 'openapi-ref-remota') copia.components.schemas.Remoto = { $ref: 'https://ajeno.ejemplo.pe/esquema.json' };
      if (modo === 'operacion-nueva') copia.paths['/nuevo-endpoint'] = { get: { responses: { 200: { description: 'ok' } } } };
      return responder(200, copia);
    }
    if (p === '/auth/login' && m === 'POST') {
      estado.intentosLogin += 1;
      if (modo === 'login401' && cuerpo.correo === sa.correo) return responder(401, { error: { codigo: 'INVALID_CREDENTIALS' } });
      const usuario = [...estado.usuarios.values()].find((u) => u.correo === String(cuerpo.correo).toLowerCase());
      if (!usuario || usuario.password !== cuerpo.password) return responder(401, { error: { codigo: 'INVALID_CREDENTIALS' } });
      if (!usuario.habilitado) return responder(403, { error: { codigo: 'ACCOUNT_DISABLED' } });
      const t = nuevoJwt(usuario.id, usuario.rol);
      estado.sesiones.set(t, { usuario: usuario.id, revocada: false });
      return responder(200, { access_token: t, token_type: 'bearer', rol: modo === 'rol-incorrecto' && usuario.id === sa.id ? 'administrador' : usuario.rol, nombre: 'x' });
    }
    if (p === '/auth/logout' && m === 'POST') {
      if (!exigir()) return undefined;
      estado.sesiones.get(token).revocada = true;
      return responder(204);
    }
    if (p === '/usuarios' && m === 'GET') { if (!exigir('superadmin')) return undefined; return responder(200, [...estado.usuarios.values()].map(({ password, ...u }) => u)); }
    if (p === '/usuarios' && m === 'POST') {
      if (!exigir('superadmin')) return undefined;
      const u = { id: `u-${estado.usuarios.size + 1}`, nombre: cuerpo.nombre, correo: String(cuerpo.correo).toLowerCase(), password: cuerpo.password, rol: 'administrador', habilitado: true };
      estado.usuarios.set(u.id, u);
      return responder(201, { id: u.id, nombre: u.nombre, correo: u.correo, rol: u.rol, habilitado: true });
    }
    let r;
    if ((r = p.match(/^\/usuarios\/([^/]+)\/deshabilitar$/)) && m === 'PATCH') {
      if (!exigir('superadmin')) return undefined;
      const u = estado.usuarios.get(r[1]);
      if (!u) return responder(404, {});
      u.habilitado = false;
      for (const s of estado.sesiones.values()) if (s.usuario === u.id) s.revocada = true;
      return responder(200, { id: u.id, habilitado: false, rol: u.rol });
    }
    if (p === '/empresas' && m === 'POST') {
      if (!exigir('superadmin')) return undefined;
      const e = { id: `e-${estado.empresas.size + 1}`, nombre: cuerpo.nombre, sector: cuerpo.sector, activa: true };
      estado.empresas.set(e.id, e);
      return responder(201, e);
    }
    if (p === '/empresas' && m === 'GET') {
      if (!exigir()) return undefined;
      // Contrato real: solo el SuperAdmin lista sin filtro; el Administrador necesita solo_activas=true.
      if (usuarioValido.rol !== 'superadmin' && url.searchParams.get('solo_activas') !== 'true') return responder(403, { error: { codigo: 'FORBIDDEN' } });
      return responder(200, [...estado.empresas.values()]);
    }
    if ((r = p.match(/^\/empresas\/([^/]+)$/)) && m === 'GET') {
      if (!exigir('superadmin')) return undefined;
      return estado.empresas.has(r[1]) ? responder(200, estado.empresas.get(r[1])) : responder(404, {});
    }
    if ((r = p.match(/^\/empresas\/([^/]+)\/desactivar$/)) && m === 'PATCH') {
      if (!exigir('superadmin')) return undefined;
      const e = estado.empresas.get(r[1]);
      if (!e) return responder(404, {});
      estado.intentosDesactivar = (estado.intentosDesactivar ?? 0) + 1;
      if (modo === 'desactivar-falla-una-vez' && estado.intentosDesactivar === 1) return responder(500, {});
      e.activa = false;
      return responder(200, e);
    }
    if (p === '/auditoria' && m === 'GET') { if (!exigir('superadmin')) return undefined; return responder(200, { eventos: [] }); }
    if (p === '/reportes' && m === 'GET') { if (!exigir('administrador')) return undefined; return responder(200, []); }
    if (p.startsWith('/analisis-gri/') && m === 'GET') { if (!exigir('administrador')) return undefined; return responder(409, { error: { codigo: 'ANALYSIS_NO_ELIGIBLE_DOCUMENTS' } }); }
    return responder(404, { detail: 'Not Found' });
  });

  return {
    estado,
    superadmin,
    escuchar: () => new Promise((resolver) => servidor.listen(0, '127.0.0.1', () => resolver(`http://127.0.0.1:${servidor.address().port}`))),
    cerrar: () => new Promise((resolver) => { servidor.closeAllConnections?.(); servidor.close(() => resolver()); }),
  };
}
