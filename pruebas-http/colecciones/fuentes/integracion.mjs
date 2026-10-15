
import { T, carpeta, coleccion, peticion } from './dsl.mjs';

const UUID_INEXISTENTE = '00000000-0000-4000-8000-000000000000';
const CORREO = (etiqueta) => `e2e-int-{{run_id}}-${etiqueta}@igualab-e2e.example.com`;
const ADMIN = (etiqueta) => ({ nombre: `E2E INT {{run_id}} ${etiqueta}`, correo: CORREO(etiqueta), password: '{{pwd_int}}' });

// ---- piezas reutilizables ----------------------------------------------------------------------------------------------

const login = (nombre, correo, password, pruebas, extra = {}) =>
  peticion({ nombre, metodo: 'POST', ruta: '/auth/login', json: { correo, password }, pruebas, ...extra });

const usuarioSintetico = (nombre, etiqueta, guardarId, guardarCorreo) =>
  peticion({
    nombre, metodo: 'POST', ruta: '/usuarios', token: 'i_token_super', json: ADMIN(etiqueta),
    pruebas: [
      T.estado(201),
      T.prueba('UsuarioResponse: id, nombre, correo, rol ADMINISTRADOR, habilitado, creado_en; sin datos de contraseña', (j) => {
        pm.expect(Object.keys(j)).to.have.members(['id', 'nombre', 'correo', 'rol', 'habilitado', 'creado_en']);
        pm.expect(esUuid(j.id)).to.equal(true);
        pm.expect(j.rol).to.equal('administrador');
        pm.expect(j.habilitado).to.equal(true);
        pm.expect(j.correo, 'el correo se guarda en minúsculas').to.equal(j.correo.toLowerCase());
        pm.expect(esFecha(j.creado_en)).to.equal(true);
        pm.expect(sinSecretos(j)).to.equal(true);
      }),
      T.guardarCampos({ [guardarId]: 'j.id', [guardarCorreo]: 'j.correo' }),
    ],
  });

const operacion = (nombre, variable) =>
  peticion({
    nombre, metodo: 'POST', ruta: '/documentos/operaciones', token: 'i_token_super',
    pruebas: [T.estado(201), T.guardarCampos({ [variable]: 'j.operacion_id' })],
  });

const ingesta = (nombre, operacionRef, campos, pruebas, token = 'i_token_super') => {
  // `operacionRef` es una variable de la colección o, si ya es un UUID, el identificador literal.
  const id = /^[0-9a-f-]{36}$/.test(operacionRef) ? operacionRef : `{{${operacionRef}}}`;
  return peticion({ nombre, metodo: 'POST', ruta: `/documentos/operaciones/${id}/ingesta`, token, formulario: campos, pruebas });
};

const camposValidos = (extra = {}) => [
  { key: 'archivo', src: extra.src ?? 'muestras/estaticas/minimo.md' },
  { key: 'empresa_id', value: extra.empresa ?? '{{i_empresa1_id}}' },
  { key: 'anio', value: extra.anio ?? '{{anio_actual}}' },
  { key: 'tipo_documento', value: extra.tipo ?? 'REPORTE_SOSTENIBILIDAD_GRI' },
];

const operacionRechazada = (nombre, operacionVar, codigo) =>
  peticion({
    nombre, ruta: `/documentos/operaciones/{{${operacionVar}}}`, token: 'i_token_super',
    pruebas: [
      T.estado(200),
      T.prueba(`la operación quedó RECHAZADA con ${codigo} y sin documento`, (j, v, a) => {
        pm.expect(j.estado).to.equal('RECHAZADA');
        pm.expect(j.terminal).to.equal(true);
        pm.expect(j.exitosa).to.equal(false);
        pm.expect(j.documento_id).to.equal(null);
        pm.expect(j.resultado_analisis).to.equal(null);
        pm.expect(j.error.code).to.equal(a.codigo);
        pm.expect(j.error.message).to.be.a('string').and.not.empty;
      }, { codigo }),
    ],
  });

// ---- carpetas ----------------------------------------------------------------------------------------------------------

const infraestructura = carpeta('01 Infraestructura', 'Disponibilidad y errores HTTP genéricos con el mismo contrato de error.', [
  peticion({
    nombre: 'GET /health responde ok', ruta: '/health',
    pruebas: [
      T.estado(200),
      T.prueba('cuerpo {status: ok}', (j) => { pm.expect(j).to.deep.equal({ status: 'ok' }); }),
      T.prueba('toda respuesta lleva X-Request-ID (UUID)', () => { pm.expect(esUuid(pm.response.headers.get('X-Request-ID'))).to.equal(true); }),
    ],
  }),
  peticion({ nombre: 'Ruta inexistente → 404 NOT_FOUND con el contrato de error', ruta: '/no-existe', pruebas: [T.error(404, 'NOT_FOUND')] }),
  peticion({ nombre: 'Método no permitido → 405 METHOD_NOT_ALLOWED', metodo: 'DELETE', ruta: '/health', pruebas: [T.error(405, 'METHOD_NOT_ALLOWED')] }),
]);

const autenticacion = carpeta('02 Autenticación', 'RF-001, RF-005/006: login, sesión, cierre de sesión, contraseñas. Las pruebas de credenciales incorrectas usan cuentas sintéticas o inexistentes: nunca la del SuperAdmin.', [
  login('Login SuperAdmin (credenciales de la ejecución)', '{{superadmin_correo}}', '{{superadmin_password}}', [
    "if (pm.response.code === 401 || pm.response.code === 423) { pm.execution.setNextRequest(null); }",
    T.estado(200),
    T.prueba('LoginResponse: access_token JWT, token_type bearer, rol SUPERADMIN, nombre', (j) => {
      pm.expect(Object.keys(j)).to.have.members(['access_token', 'token_type', 'rol', 'nombre']);
      pm.expect(esJwt(j.access_token)).to.equal(true);
      pm.expect(j.token_type).to.equal('bearer');
      pm.expect(j.rol).to.equal('superadmin');
      pm.expect(j.nombre).to.be.a('string').and.not.empty;
    }),
    T.guardar((j, set) => set('i_token_super', j.access_token)),
  ]),
  peticion({
    nombre: 'Login con correo inexistente → 401 INVALID_CREDENTIALS y eco de X-Request-ID', metodo: 'POST', ruta: '/auth/login',
    cabeceras: { 'X-Request-ID': '{{i_rid}}' },
    previo: (v, set) => set('i_rid', pm.variables.replaceIn('{{$guid}}')),
    json: { correo: 'no-existe-{{run_id}}@igualab-e2e.example.com', password: '{{pwd_int}}' },
    pruebas: [
      T.error(401, 'INVALID_CREDENTIALS', (d) => { pm.expect(d).to.equal(null); }),
      T.prueba('el servidor devuelve el X-Request-ID enviado por el cliente', (j, v) => { pm.expect(j.error.request_id).to.equal(v('i_rid')); }),
      T.guardar((j, set) => set('i_mensaje_credenciales', j.error.message)),
    ],
  }),
  login('Login con correo mal formado → 422', 'esto-no-es-un-correo', 'x', [T.validacion('correo')]),
  peticion({
    nombre: 'Login sin cuerpo → 422', metodo: 'POST', ruta: '/auth/login',
    pruebas: [T.validacion('body')],
  }),
  usuarioSintetico('Crear Administrador sintético 1 (SuperAdmin)', 'a1', 'i_admin_id', 'i_admin_correo'),
  login('Login Administrador sintético', '{{i_admin_correo}}', '{{pwd_int}}', [
    T.estado(200),
    T.prueba('rol ADMINISTRADOR', (j) => { pm.expect(j.rol).to.equal('administrador'); pm.expect(esJwt(j.access_token)).to.equal(true); }),
    T.guardar((j, set) => set('i_token_admin', j.access_token)),
  ]),
  login('Login con contraseña incorrecta (cuenta sintética) → 401 sin distinguir de «no existe»', '{{i_admin_correo}}', 'Incorrecta#2026x', [
    T.error(401, 'INVALID_CREDENTIALS'),
    T.prueba('mismo mensaje que para una cuenta inexistente (no revela qué cuentas existen)', (j, v) => { pm.expect(j.error.message).to.equal(v('i_mensaje_credenciales')); }),
  ]),
  peticion({ nombre: 'Cambiar mi contraseña sin sesión → 401', metodo: 'PATCH', ruta: '/auth/mi-contrasena', json: { password_actual: 'x', password_nueva: 'y' }, pruebas: [T.sinSesion()] }),
  peticion({
    nombre: 'Cambiar mi contraseña con la actual incorrecta → 401 CURRENT_PASSWORD_INVALID', metodo: 'PATCH', ruta: '/auth/mi-contrasena', token: 'i_token_admin',
    json: { password_actual: 'Incorrecta#2026x', password_nueva: '{{pwd_int_nueva}}' }, pruebas: [T.error(401, 'CURRENT_PASSWORD_INVALID')],
  }),
  peticion({
    nombre: 'Cambiar mi contraseña con una débil → 400 PASSWORD_POLICY_VIOLATION', metodo: 'PATCH', ruta: '/auth/mi-contrasena', token: 'i_token_admin',
    json: { password_actual: '{{pwd_int}}', password_nueva: 'abc' },
    pruebas: [T.error(400, 'PASSWORD_POLICY_VIOLATION', (d) => {
      pm.expect(d.errors).to.be.an('array').that.is.not.empty;
      d.errors.forEach((e) => pm.expect(e).to.be.a('string'));
    })],
  }),
  peticion({
    nombre: 'Cambiar mi contraseña (válida) → 200', metodo: 'PATCH', ruta: '/auth/mi-contrasena', token: 'i_token_admin',
    json: { password_actual: '{{pwd_int}}', password_nueva: '{{pwd_int_nueva}}' },
    pruebas: [T.estado(200), T.prueba('mensaje de confirmación', (j) => { pm.expect(Object.keys(j)).to.have.members(['mensaje']); pm.expect(j.mensaje).to.be.a('string').and.not.empty; })],
  }),
  login('Login con la contraseña anterior → 401 INVALID_CREDENTIALS', '{{i_admin_correo}}', '{{pwd_int}}', [T.error(401, 'INVALID_CREDENTIALS')]),
  login('Login con la contraseña nueva → 200', '{{i_admin_correo}}', '{{pwd_int_nueva}}', [
    T.estado(200), T.guardar((j, set) => set('i_token_admin_cierre', j.access_token)),
  ]),
  peticion({ nombre: 'Logout con la sesión nueva → 204 sin cuerpo', metodo: 'POST', ruta: '/auth/logout', token: 'i_token_admin_cierre', pruebas: [T.estado(204), T.prueba('sin cuerpo', () => { pm.expect(pm.response.text()).to.equal(''); })] }),
  peticion({ nombre: 'El token del logout ya no sirve → 401 INVALID_SESSION', ruta: '/empresas', consulta: { solo_activas: 'true' }, token: 'i_token_admin_cierre', pruebas: [T.sinSesion()] }),
  peticion({ nombre: 'Logout sin token → 401', metodo: 'POST', ruta: '/auth/logout', pruebas: [T.sinSesion()] }),
  login('Login Administrador sintético (sesión para el resto de la colección)', '{{i_admin_correo}}', '{{pwd_int_nueva}}', [
    T.estado(200), T.guardar((j, set) => set('i_token_admin', j.access_token)),
  ]),
  peticion({
    nombre: 'Recuperar contraseña con un correo inexistente → 202 genérico (no revela la cuenta ni envía correo)', metodo: 'POST', ruta: '/auth/recuperar-contrasena',
    json: { correo: 'no-existe-{{run_id}}@igualab-e2e.example.com' },
    pruebas: [T.estado(202), T.prueba('mensaje genérico', (j) => { pm.expect(Object.keys(j)).to.have.members(['mensaje']); pm.expect(j.mensaje).to.include('Si el correo está registrado'); })],
  }),
  peticion({ nombre: 'Recuperar contraseña con un correo mal formado → 422', metodo: 'POST', ruta: '/auth/recuperar-contrasena', json: { correo: 'no-es-correo' }, pruebas: [T.validacion('correo')] }),
  peticion({
    nombre: 'Restablecer contraseña con un token inválido → 400 INVALID_OR_EXPIRED_RESET_TOKEN', metodo: 'POST', ruta: '/auth/restablecer-contrasena',
    json: { token: 'token-sintetico-invalido', password_nueva: '{{pwd_int_nueva}}', password_nueva_confirmacion: '{{pwd_int_nueva}}' },
    pruebas: [T.error(400, 'INVALID_OR_EXPIRED_RESET_TOKEN')],
  }),
  peticion({
    nombre: 'Restablecer contraseña con confirmación distinta → 422', metodo: 'POST', ruta: '/auth/restablecer-contrasena',
    json: { token: 'token-sintetico-invalido', password_nueva: '{{pwd_int_nueva}}', password_nueva_confirmacion: 'otra-distinta' },
    pruebas: [T.validacion('password_nueva_confirmacion')],
  }),
  peticion({ nombre: 'Token malformado → 401 INVALID_SESSION', ruta: '/usuarios', cabeceras: { Authorization: 'Bearer esto.no.es-un-jwt' }, pruebas: [T.sinSesion()] }),
  peticion({ nombre: 'Esquema de autorización distinto de Bearer → 401 INVALID_SESSION', ruta: '/usuarios', cabeceras: { Authorization: 'Basic dXN1YXJpbzpjbGF2ZQ==' }, pruebas: [T.sinSesion()] }),
]);

const usuarios = carpeta('03 Usuarios', 'RF-010…RF-015: alta, listado, habilitar/deshabilitar y rechazos de la transferencia. Solo SuperAdmin. NUNCA se transfiere el rol.', [
  peticion({
    nombre: 'Listar usuarios (SuperAdmin) → 200', ruta: '/usuarios', token: 'i_token_super',
    pruebas: [
      T.estado(200),
      T.prueba('lista de UsuarioResponse sin datos de contraseña', (j) => {
        pm.expect(j).to.be.an('array').that.is.not.empty;
        j.forEach((u) => {
          pm.expect(Object.keys(u)).to.have.members(['id', 'nombre', 'correo', 'rol', 'habilitado', 'creado_en']);
          pm.expect(sinSecretos(u)).to.equal(true);
        });
      }),
      T.prueba('incluye la cuenta sintética y hay exactamente un SuperAdmin', (j, v) => {
        const sintetica = j.find((u) => u.id === v('i_admin_id'));
        pm.expect(sintetica, 'cuenta sintética creada en esta ejecución').to.be.an('object');
        pm.expect(sintetica.habilitado).to.equal(true);
        pm.expect(j.filter((u) => u.rol === 'superadmin')).to.have.length(1);
      }),
      T.guardar((j, set, v) => set('i_superadmin_id', j.find((u) => u.rol === 'superadmin').id)),
    ],
  }),
  usuarioSintetico('Crear Administrador sintético 2 (el correo se normaliza a minúsculas)', 'A2', 'i_admin2_id', 'i_admin2_correo'),
  peticion({
    nombre: 'Crear con el mismo correo en otra capitalización → 409 EMAIL_ALREADY_EXISTS', metodo: 'POST', ruta: '/usuarios', token: 'i_token_super',
    json: { nombre: 'Duplicado', correo: CORREO('A2').toUpperCase().replace('{{RUN_ID}}', '{{run_id}}'), password: '{{pwd_int}}' },
    pruebas: [T.error(409, 'EMAIL_ALREADY_EXISTS')],
  }),
  peticion({
    nombre: 'Crear con contraseña débil → 400 PASSWORD_POLICY_VIOLATION', metodo: 'POST', ruta: '/usuarios', token: 'i_token_super',
    json: { nombre: 'Débil', correo: CORREO('debil'), password: 'abc' },
    pruebas: [T.error(400, 'PASSWORD_POLICY_VIOLATION', (d) => { pm.expect(d.errors).to.be.an('array').that.is.not.empty; })],
  }),
  peticion({ nombre: 'Crear con correo mal formado → 422', metodo: 'POST', ruta: '/usuarios', token: 'i_token_super', json: { nombre: 'X', correo: 'no-es-correo', password: '{{pwd_int}}' }, pruebas: [T.validacion('correo')] }),
  peticion({ nombre: 'Crear sin nombre → 422', metodo: 'POST', ruta: '/usuarios', token: 'i_token_super', json: { correo: CORREO('sin-nombre'), password: '{{pwd_int}}' }, pruebas: [T.validacion('nombre')] }),
  peticion({ nombre: 'Administrador intenta crear usuarios → 403 FORBIDDEN', metodo: 'POST', ruta: '/usuarios', token: 'i_token_admin', json: ADMIN('prohibido'), pruebas: [T.prohibido()] }),
  peticion({ nombre: 'Administrador intenta listar usuarios → 403 FORBIDDEN', ruta: '/usuarios', token: 'i_token_admin', pruebas: [T.prohibido()] }),
  peticion({ nombre: 'Crear usuario sin sesión → 401', metodo: 'POST', ruta: '/usuarios', json: ADMIN('anonimo'), pruebas: [T.sinSesion()] }),
  peticion({
    nombre: 'Deshabilitar la cuenta sintética 2 → 200 habilitado=false', metodo: 'PATCH', ruta: '/usuarios/{{i_admin2_id}}/deshabilitar', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('cuenta deshabilitada', (j, v) => { pm.expect(j.id).to.equal(v('i_admin2_id')); pm.expect(j.habilitado).to.equal(false); pm.expect(j.rol).to.equal('administrador'); })],
  }),
  peticion({
    nombre: 'Deshabilitar de nuevo es idempotente → 200', metodo: 'PATCH', ruta: '/usuarios/{{i_admin2_id}}/deshabilitar', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('sigue deshabilitada', (j) => { pm.expect(j.habilitado).to.equal(false); })],
  }),
  login('Login de la cuenta deshabilitada → 403 ACCOUNT_DISABLED', '{{i_admin2_correo}}', '{{pwd_int}}', [T.error(403, 'ACCOUNT_DISABLED')]),
  peticion({
    nombre: 'Rechazo de transferencia: la cuenta destino está deshabilitada → 409 DESTINATION_USER_DISABLED', metodo: 'POST', ruta: '/usuarios/transferir-superadmin', token: 'i_token_super',
    json: { cuenta_destino_id: '{{i_admin2_id}}' }, pruebas: [T.error(409, 'DESTINATION_USER_DISABLED')],
  }),
  peticion({
    nombre: 'Habilitar la cuenta sintética 2 → 200 habilitado=true', metodo: 'PATCH', ruta: '/usuarios/{{i_admin2_id}}/habilitar', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('cuenta habilitada', (j) => { pm.expect(j.habilitado).to.equal(true); })],
  }),
  peticion({ nombre: 'Deshabilitar un usuario inexistente → 404 USER_NOT_FOUND', metodo: 'PATCH', ruta: `/usuarios/${UUID_INEXISTENTE}/deshabilitar`, token: 'i_token_super', pruebas: [T.error(404, 'USER_NOT_FOUND')] }),
  peticion({ nombre: 'Deshabilitar con un id que no es UUID → 422', metodo: 'PATCH', ruta: '/usuarios/no-es-uuid/deshabilitar', token: 'i_token_super', pruebas: [T.validacion('usuario_id')] }),
  peticion({
    nombre: 'No se puede deshabilitar al SuperAdmin → 409 SUPERADMIN_CANNOT_BE_DISABLED', metodo: 'PATCH', ruta: '/usuarios/{{i_superadmin_id}}/deshabilitar', token: 'i_token_super',
    pruebas: [T.error(409, 'SUPERADMIN_CANNOT_BE_DISABLED')],
  }),
  peticion({ nombre: 'Administrador intenta deshabilitar → 403 FORBIDDEN', metodo: 'PATCH', ruta: '/usuarios/{{i_admin2_id}}/deshabilitar', token: 'i_token_admin', pruebas: [T.prohibido()] }),
  peticion({
    nombre: 'Rechazo de transferencia: la cuenta destino no existe → 404 DESTINATION_USER_NOT_FOUND', metodo: 'POST', ruta: '/usuarios/transferir-superadmin', token: 'i_token_super',
    json: { cuenta_destino_id: UUID_INEXISTENTE }, pruebas: [T.error(404, 'DESTINATION_USER_NOT_FOUND')],
  }),
  peticion({
    nombre: 'Rechazo de transferencia: el destino ya es SuperAdmin → 409 DESTINATION_ALREADY_SUPERADMIN', metodo: 'POST', ruta: '/usuarios/transferir-superadmin', token: 'i_token_super',
    json: { cuenta_destino_id: '{{i_superadmin_id}}' }, pruebas: [T.error(409, 'DESTINATION_ALREADY_SUPERADMIN')],
  }),
  peticion({ nombre: 'Transferir con un cuerpo inválido → 422', metodo: 'POST', ruta: '/usuarios/transferir-superadmin', token: 'i_token_super', json: { cuenta_destino_id: 'no-es-uuid' }, pruebas: [T.validacion('cuenta_destino_id')] }),
  peticion({ nombre: 'Administrador intenta transferir → 403 FORBIDDEN', metodo: 'POST', ruta: '/usuarios/transferir-superadmin', token: 'i_token_admin', json: { cuenta_destino_id: '{{i_admin_id}}' }, pruebas: [T.prohibido()] }),
  peticion({
    nombre: 'Verificación final: el SuperAdmin sigue siendo el mismo y único', ruta: '/usuarios', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('ningún rol se transfirió', (j, v) => {
      const supers = j.filter((u) => u.rol === 'superadmin');
      pm.expect(supers).to.have.length(1);
      pm.expect(supers[0].id).to.equal(v('i_superadmin_id'));
      pm.expect(supers[0].correo).to.equal(String(v('superadmin_correo')).toLowerCase());
    })],
  }),
]);

const empresaNueva = (nombre, nombreEmpresa, sector, guardarId) =>
  peticion({
    nombre, metodo: 'POST', ruta: '/empresas', token: 'i_token_super', json: { nombre: nombreEmpresa, sector },
    pruebas: [
      T.estado(201),
      T.prueba('EmpresaResponse: activa por defecto, sector, id y fecha', (j, v, a) => {
        pm.expect(Object.keys(j)).to.have.members(['id', 'nombre', 'sector', 'activa', 'creada_en']);
        pm.expect(esUuid(j.id)).to.equal(true);
        pm.expect(j.activa).to.equal(true);
        pm.expect(j.sector).to.equal(a.sector);
        pm.expect(esFecha(j.creada_en)).to.equal(true);
      }, { sector }),
      T.guardarCampos({ [guardarId]: 'j.id' }),
    ],
  });

const empresas = carpeta('04 Empresas', 'RF-040…: catálogo de empresas. Crear/editar/activar/desactivar y consultar por id: solo SuperAdmin; el Administrador solo lista activas.', [
  peticion({
    nombre: 'Crear empresa 1 (el nombre se recorta y nace activa)', metodo: 'POST', ruta: '/empresas', token: 'i_token_super',
    json: { nombre: '  E2E INT {{run_id}} Empresa 1  ', sector: 'ENERGIA' },
    pruebas: [
      T.estado(201),
      T.prueba('EmpresaResponse con el nombre recortado', (j, v) => {
        pm.expect(Object.keys(j)).to.have.members(['id', 'nombre', 'sector', 'activa', 'creada_en']);
        pm.expect(j.nombre).to.equal('E2E INT ' + v('run_id') + ' Empresa 1');
        pm.expect(j.sector).to.equal('ENERGIA');
        pm.expect(j.activa).to.equal(true);
        pm.expect(esUuid(j.id)).to.equal(true);
      }),
      T.guardar((j, set) => { set('i_empresa1_id', j.id); set('i_empresa1_nombre', j.nombre); }),
    ],
  }),
  empresaNueva('Crear empresa 2 (PETROLEO)', 'E2E INT {{run_id}} Empresa 2', 'PETROLEO', 'i_empresa2_id'),
  peticion({
    nombre: 'Crear con el mismo nombre en otra capitalización → 409 COMPANY_ALREADY_EXISTS', metodo: 'POST', ruta: '/empresas', token: 'i_token_super',
    json: { nombre: 'e2e int {{run_id}} EMPRESA 1', sector: 'ENERGIA' }, pruebas: [T.error(409, 'COMPANY_ALREADY_EXISTS')],
  }),
  peticion({ nombre: 'Crear con un sector inválido → 422', metodo: 'POST', ruta: '/empresas', token: 'i_token_super', json: { nombre: 'E2E INT {{run_id}} X', sector: 'AGRICULTURA' }, pruebas: [T.validacion('sector')] }),
  peticion({ nombre: 'Crear con nombre vacío → 422', metodo: 'POST', ruta: '/empresas', token: 'i_token_super', json: { nombre: '   ', sector: 'MINERIA' }, pruebas: [T.validacion('nombre')] }),
  peticion({ nombre: 'Crear con un campo no permitido → 422', metodo: 'POST', ruta: '/empresas', token: 'i_token_super', json: { nombre: 'E2E INT {{run_id}} Y', sector: 'MINERIA', activa: false }, pruebas: [T.validacion('activa')] }),
  peticion({ nombre: 'Administrador intenta crear una empresa → 403 FORBIDDEN', metodo: 'POST', ruta: '/empresas', token: 'i_token_admin', json: { nombre: 'E2E INT {{run_id}} Z', sector: 'MINERIA' }, pruebas: [T.prohibido()] }),
  peticion({ nombre: 'Crear empresa sin sesión → 401', metodo: 'POST', ruta: '/empresas', json: { nombre: 'E2E INT {{run_id}} W', sector: 'MINERIA' }, pruebas: [T.sinSesion()] }),
  peticion({
    nombre: 'Listar todas (SuperAdmin) incluye las empresas creadas', ruta: '/empresas', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('lista de EmpresaResponse con las dos empresas sintéticas', (j, v) => {
      pm.expect(j).to.be.an('array');
      pm.expect(j.map((e) => e.id)).to.include.members([v('i_empresa1_id'), v('i_empresa2_id')]);
    })],
  }),
  peticion({
    nombre: 'Listar filtrando por sector → solo ese sector', ruta: '/empresas', consulta: { sector: 'PETROLEO' }, token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('todas PETROLEO; incluye la empresa 2 y excluye la 1', (j, v) => {
      j.forEach((e) => pm.expect(e.sector).to.equal('PETROLEO'));
      pm.expect(j.map((e) => e.id)).to.include(v('i_empresa2_id'));
      pm.expect(j.map((e) => e.id)).to.not.include(v('i_empresa1_id'));
    })],
  }),
  peticion({ nombre: 'Listar con un sector inválido → 422', ruta: '/empresas', consulta: { sector: 'AGRICULTURA' }, token: 'i_token_super', pruebas: [T.validacion('sector')] }),
  peticion({
    nombre: 'Administrador lista solo activas → 200', ruta: '/empresas', consulta: { solo_activas: 'true' }, token: 'i_token_admin',
    pruebas: [T.estado(200), T.prueba('todas activas e incluye la empresa 1', (j, v) => {
      j.forEach((e) => pm.expect(e.activa).to.equal(true));
      pm.expect(j.map((e) => e.id)).to.include(v('i_empresa1_id'));
    })],
  }),
  peticion({ nombre: 'Administrador lista sin filtrar → 403 FORBIDDEN (solo puede ver activas)', ruta: '/empresas', token: 'i_token_admin', pruebas: [T.prohibido()] }),
  peticion({ nombre: 'Listar sin sesión → 401', ruta: '/empresas', pruebas: [T.sinSesion()] }),
  peticion({
    nombre: 'Obtener la empresa 1 por id → 200', ruta: '/empresas/{{i_empresa1_id}}', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('es la empresa creada', (j, v) => { pm.expect(j.id).to.equal(v('i_empresa1_id')); pm.expect(j.nombre).to.equal(v('i_empresa1_nombre')); pm.expect(j.sector).to.equal('ENERGIA'); })],
  }),
  peticion({ nombre: 'Obtener una empresa inexistente → 404 COMPANY_NOT_FOUND', ruta: `/empresas/${UUID_INEXISTENTE}`, token: 'i_token_super', pruebas: [T.error(404, 'COMPANY_NOT_FOUND')] }),
  peticion({ nombre: 'Obtener con un id que no es UUID → 422', ruta: '/empresas/no-es-uuid', token: 'i_token_super', pruebas: [T.validacion('empresa_id')] }),
  peticion({ nombre: 'Administrador intenta obtener por id → 403 FORBIDDEN', ruta: '/empresas/{{i_empresa1_id}}', token: 'i_token_admin', pruebas: [T.prohibido()] }),
  peticion({
    nombre: 'Editar el sector de la empresa 2 → 200', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}', token: 'i_token_super', json: { sector: 'MINERIA' },
    pruebas: [T.estado(200), T.prueba('sector actualizado y el nombre intacto', (j, v) => { pm.expect(j.sector).to.equal('MINERIA'); pm.expect(j.nombre).to.equal('E2E INT ' + v('run_id') + ' Empresa 2'); })],
  }),
  peticion({
    nombre: 'Editar con el nombre de otra empresa → 409 COMPANY_ALREADY_EXISTS', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}', token: 'i_token_super', json: { nombre: '{{i_empresa1_nombre}}' },
    pruebas: [T.error(409, 'COMPANY_ALREADY_EXISTS')],
  }),
  peticion({ nombre: 'Editar con un cuerpo vacío → 422', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}', token: 'i_token_super', json: {}, pruebas: [T.validacion()] }),
  peticion({ nombre: 'Editar con un campo nulo → 422', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}', token: 'i_token_super', json: { nombre: null }, pruebas: [T.validacion()] }),
  peticion({ nombre: 'Editar una empresa inexistente → 404 COMPANY_NOT_FOUND', metodo: 'PATCH', ruta: `/empresas/${UUID_INEXISTENTE}`, token: 'i_token_super', json: { sector: 'MINERIA' }, pruebas: [T.error(404, 'COMPANY_NOT_FOUND')] }),
  peticion({ nombre: 'Administrador intenta editar → 403 FORBIDDEN', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}', token: 'i_token_admin', json: { sector: 'MINERIA' }, pruebas: [T.prohibido()] }),
  peticion({
    nombre: 'Desactivar la empresa 2 → 200 activa=false', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}/desactivar', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('inactiva', (j, v) => { pm.expect(j.id).to.equal(v('i_empresa2_id')); pm.expect(j.activa).to.equal(false); })],
  }),
  peticion({
    nombre: 'Las inactivas no salen en «solo activas»', ruta: '/empresas', consulta: { solo_activas: 'true' }, token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('la empresa 2 no aparece y la 1 sí', (j, v) => {
      pm.expect(j.map((e) => e.id)).to.not.include(v('i_empresa2_id'));
      pm.expect(j.map((e) => e.id)).to.include(v('i_empresa1_id'));
    })],
  }),
  peticion({
    nombre: 'Activar la empresa 2 → 200 activa=true', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}/activar', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('activa', (j) => { pm.expect(j.activa).to.equal(true); })],
  }),
  peticion({ nombre: 'Activar una empresa inexistente → 404 COMPANY_NOT_FOUND', metodo: 'PATCH', ruta: `/empresas/${UUID_INEXISTENTE}/activar`, token: 'i_token_super', pruebas: [T.error(404, 'COMPANY_NOT_FOUND')] }),
  peticion({ nombre: 'Administrador intenta desactivar → 403 FORBIDDEN', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}/desactivar', token: 'i_token_admin', pruebas: [T.prohibido()] }),
]);

const documentos = carpeta('05 Documentos', 'Ingesta (solo SuperAdmin). Esta carpeta NO ingiere documentos: prueba el contrato de operaciones, listados y los rechazos que ocurren antes de cualquier servicio externo (sin OCI).', [
  peticion({ nombre: 'Crear operación sin sesión → 401', metodo: 'POST', ruta: '/documentos/operaciones', pruebas: [T.sinSesion()] }),
  peticion({ nombre: 'Administrador intenta crear una operación → 403 FORBIDDEN', metodo: 'POST', ruta: '/documentos/operaciones', token: 'i_token_admin', pruebas: [T.prohibido()] }),
  peticion({
    nombre: 'Crear operación (SuperAdmin) → 201 CREADA', metodo: 'POST', ruta: '/documentos/operaciones', token: 'i_token_super',
    pruebas: [
      T.estado(201),
      T.prueba('OperacionCreadaResponse con sus rutas', (j) => {
        pm.expect(Object.keys(j)).to.have.members(['operacion_id', 'estado', 'creada_en', 'ingesta_url', 'progreso_url']);
        pm.expect(esUuid(j.operacion_id)).to.equal(true);
        pm.expect(j.estado).to.equal('CREADA');
        pm.expect(j.ingesta_url).to.equal('/documentos/operaciones/' + j.operacion_id + '/ingesta');
        pm.expect(j.progreso_url).to.equal('/documentos/operaciones/' + j.operacion_id);
      }),
      T.guardar((j, set) => set('i_op_creada', j.operacion_id)),
    ],
  }),
  peticion({
    nombre: 'Consultar la operación recién creada → 200 CREADA, sin documento', ruta: '/documentos/operaciones/{{i_op_creada}}', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('OperacionResponse: no terminal, no exitosa, sin documento ni análisis', (j, v) => {
      pm.expect(j.operacion_id).to.equal(v('i_op_creada'));
      pm.expect(j.estado).to.equal('CREADA');
      pm.expect(j.terminal).to.equal(false);
      pm.expect(j.exitosa).to.equal(false);
      pm.expect(j.documento_id).to.equal(null);
      pm.expect(j.resultado_analisis).to.equal(null);
      pm.expect(j.fragmentos_procesados).to.equal(0);
      pm.expect(j.error).to.equal(null);
      pm.expect(esFecha(j.creada_en) && esFecha(j.actualizada_en)).to.equal(true);
    })],
  }),
  peticion({ nombre: 'Consultar una operación inexistente → 404 OPERATION_NOT_FOUND', ruta: `/documentos/operaciones/${UUID_INEXISTENTE}`, token: 'i_token_super', pruebas: [T.error(404, 'OPERATION_NOT_FOUND')] }),
  peticion({ nombre: 'Consultar una operación con un id que no es UUID → 422', ruta: '/documentos/operaciones/no-es-uuid', token: 'i_token_super', pruebas: [T.validacion('operacion_id')] }),
  peticion({ nombre: 'Administrador intenta consultar una operación → 403 FORBIDDEN', ruta: '/documentos/operaciones/{{i_op_creada}}', token: 'i_token_admin', pruebas: [T.prohibido()] }),
  peticion({
    nombre: 'Reintentar la publicación de una operación sin documento → 409 OPERATION_WITHOUT_DOCUMENT', metodo: 'POST', ruta: '/documentos/operaciones/{{i_op_creada}}/reintentar-publicacion', token: 'i_token_super',
    pruebas: [T.error(409, 'OPERATION_WITHOUT_DOCUMENT')],
  }),
  peticion({ nombre: 'Reintentar la publicación de una operación inexistente → 404', metodo: 'POST', ruta: `/documentos/operaciones/${UUID_INEXISTENTE}/reintentar-publicacion`, token: 'i_token_super', pruebas: [T.error(404, 'OPERATION_NOT_FOUND')] }),
  peticion({ nombre: 'Administrador intenta reintentar una publicación → 403 FORBIDDEN', metodo: 'POST', ruta: '/documentos/operaciones/{{i_op_creada}}/reintentar-publicacion', token: 'i_token_admin', pruebas: [T.prohibido()] }),

  // --- Rechazos de la carga: ninguno llega a OCI ---------------------------------------------------------------------
  operacion('Preparar operación para un archivo con extensión no permitida', 'i_op_txt'),
  ingesta('Ingesta con extensión .txt → 400 INVALID_FILE_TYPE', 'i_op_txt', camposValidos({ src: 'muestras/estaticas/archivo.txt' }), [T.error(400, 'INVALID_FILE_TYPE')]),
  operacionRechazada('La operación del archivo .txt quedó RECHAZADA', 'i_op_txt', 'INVALID_FILE_TYPE'),
  ingesta('Reutilizar la operación ya usada → 409 OPERATION_ALREADY_STARTED', 'i_op_txt', camposValidos({ src: 'muestras/estaticas/archivo.txt' }), [T.error(409, 'OPERATION_ALREADY_STARTED')]),
  operacion('Preparar operación para un archivo vacío', 'i_op_vacio'),
  ingesta('Ingesta de un archivo vacío → 400 EMPTY_DOCUMENT', 'i_op_vacio', camposValidos({ src: 'muestras/estaticas/vacio.md' }), [T.error(400, 'EMPTY_DOCUMENT')]),
  operacion('Preparar operación para un archivo que no es UTF-8', 'i_op_enc'),
  ingesta('Ingesta de un .md que no es UTF-8 → 400 INVALID_ENCODING', 'i_op_enc', camposValidos({ src: 'muestras/estaticas/no-utf8.md' }), [T.error(400, 'INVALID_ENCODING')]),
  operacion('Preparar operación para una empresa inexistente', 'i_op_empresa'),
  ingesta('Ingesta para una empresa inexistente → 404 COMPANY_NOT_FOUND', 'i_op_empresa', camposValidos({ empresa: UUID_INEXISTENTE }), [T.error(404, 'COMPANY_NOT_FOUND')]),
  ingesta('Ingesta con una operación inexistente → 404 OPERATION_NOT_FOUND', UUID_INEXISTENTE, camposValidos(), [T.error(404, 'OPERATION_NOT_FOUND')]),
  operacion('Preparar operación para los metadatos inválidos', 'i_op_meta'),
  ingesta('Ingesta con año no numérico → 422', 'i_op_meta', camposValidos({ anio: 'abc' }), [T.validacion('anio')]),
  ingesta('Ingesta con año anterior a 2000 → 422', 'i_op_meta', camposValidos({ anio: '1999' }), [T.validacion('anio')]),
  ingesta('Ingesta con año futuro → 422', 'i_op_meta', camposValidos({ anio: '{{anio_futuro}}' }), [T.validacion('anio')]),
  ingesta('Ingesta con un tipo de documento inválido → 422', 'i_op_meta', camposValidos({ tipo: 'INFORME_X' }), [T.validacion('tipo_documento')]),
  ingesta('Ingesta con un empresa_id que no es UUID → 422', 'i_op_meta', camposValidos({ empresa: 'no-es-uuid' }), [T.validacion('empresa_id')]),
  ingesta('Ingesta sin archivo → 422', 'i_op_meta', camposValidos().filter((c) => c.key !== 'archivo'), [T.validacion('archivo')]),
  peticion({
    nombre: 'Los metadatos inválidos no consumen la operación (sigue CREADA)', ruta: '/documentos/operaciones/{{i_op_meta}}', token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('CREADA y sin error', (j) => { pm.expect(j.estado).to.equal('CREADA'); pm.expect(j.error).to.equal(null); })],
  }),
  ingesta('Administrador intenta ingerir → 403 FORBIDDEN', 'i_op_meta', camposValidos(), [T.prohibido()], 'i_token_admin'),
  peticion({
    nombre: 'Ingesta sin sesión → 401', metodo: 'POST', ruta: '/documentos/operaciones/{{i_op_meta}}/ingesta', formulario: camposValidos(),
    pruebas: [T.sinSesion()],
  }),

  // --- Listado y detalle ------------------------------------------------------------------------------------------------
  peticion({
    nombre: 'Listar documentos de una empresa sin documentos → 200 vacío', ruta: '/documentos', consulta: { empresa_id: '{{i_empresa1_id}}' }, token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('PaginaDocumentos vacía con la paginación por defecto', (j) => {
      pm.expect(Object.keys(j)).to.have.members(['items', 'total', 'pagina', 'tamano', 'paginas']);
      pm.expect(j.items).to.deep.equal([]);
      pm.expect([j.total, j.pagina, j.tamano, j.paginas]).to.deep.equal([0, 1, 20, 0]);
    })],
  }),
  peticion({
    nombre: 'Listar con filtros válidos → 200', ruta: '/documentos', consulta: { empresa_id: '{{i_empresa1_id}}', anio: '{{anio_actual}}', tipo: 'REPORTE_SOSTENIBILIDAD_GRI', estado: 'COMPLETADO', pagina: '1', tamano: '5' }, token: 'i_token_super',
    pruebas: [T.estado(200), T.prueba('respeta el tamaño de página pedido', (j) => { pm.expect(j.tamano).to.equal(5); pm.expect(j.total).to.equal(0); })],
  }),
  peticion({ nombre: 'Listar con tamaño 0 → 422', ruta: '/documentos', consulta: { tamano: '0' }, token: 'i_token_super', pruebas: [T.validacion('tamano')] }),
  peticion({ nombre: 'Listar con tamaño mayor al máximo (101) → 422', ruta: '/documentos', consulta: { tamano: '101' }, token: 'i_token_super', pruebas: [T.validacion('tamano')] }),
  peticion({ nombre: 'Listar con página 0 → 422', ruta: '/documentos', consulta: { pagina: '0' }, token: 'i_token_super', pruebas: [T.validacion('pagina')] }),
  peticion({ nombre: 'Listar con año 1999 → 422', ruta: '/documentos', consulta: { anio: '1999' }, token: 'i_token_super', pruebas: [T.validacion('anio')] }),
  peticion({ nombre: 'Listar con un estado inválido → 422', ruta: '/documentos', consulta: { estado: 'TERMINADO' }, token: 'i_token_super', pruebas: [T.validacion('estado')] }),
  peticion({ nombre: 'Listar con un tipo inválido → 422', ruta: '/documentos', consulta: { tipo: 'INFORME_X' }, token: 'i_token_super', pruebas: [T.validacion('tipo')] }),
  peticion({ nombre: 'Listar con empresa_id inválido → 422', ruta: '/documentos', consulta: { empresa_id: 'no-es-uuid' }, token: 'i_token_super', pruebas: [T.validacion('empresa_id')] }),
  peticion({ nombre: 'Administrador intenta listar documentos → 403 FORBIDDEN', ruta: '/documentos', token: 'i_token_admin', pruebas: [T.prohibido()] }),
  peticion({ nombre: 'Listar documentos sin sesión → 401', ruta: '/documentos', pruebas: [T.sinSesion()] }),
  peticion({ nombre: 'Detalle de un documento inexistente → 404 DOCUMENT_NOT_FOUND', ruta: `/documentos/${UUID_INEXISTENTE}`, token: 'i_token_super', pruebas: [T.error(404, 'DOCUMENT_NOT_FOUND')] }),
  peticion({ nombre: 'Detalle con un id que no es UUID → 422', ruta: '/documentos/no-es-uuid', token: 'i_token_super', pruebas: [T.validacion('documento_id')] }),
  peticion({ nombre: 'Administrador intenta ver un detalle → 403 FORBIDDEN', ruta: `/documentos/${UUID_INEXISTENTE}`, token: 'i_token_admin', pruebas: [T.prohibido()] }),
  peticion({ nombre: 'Detalle sin sesión → 401', ruta: `/documentos/${UUID_INEXISTENTE}`, pruebas: [T.sinSesion()] }),
]);

const BASE_GRI = '/analisis-gri/empresas/{{i_empresa1_id}}/anios/{{anio_actual}}';
const sinSesionGri = (nombre, metodo, sufijo, json) => peticion({ nombre, metodo, ruta: BASE_GRI + sufijo, json, pruebas: [T.sinSesion()] });
const superAdminGri = (nombre, metodo, sufijo, json) => peticion({ nombre, metodo, ruta: BASE_GRI + sufijo, token: 'i_token_super', json, pruebas: [T.prohibido()] });

const HUELLA_FALSA = 'a'.repeat(64);

const analisisGri = carpeta('06 Análisis GRI', 'Módulo 5 (solo Administrador; el SuperAdmin recibe 403). Sin documentos elegibles: se prueba el contrato de consulta, validaciones y rechazos. El flujo completo con datos está en la colección E2E (flujo B).', [
  sinSesionGri('Consultar sin sesión → 401', 'GET', ''),
  sinSesionGri('Asignar estado sin sesión → 401', 'PUT', '/codigos/305:2016/estado', { estado: 'OK', version_fila_esperada: 0 }),
  sinSesionGri('Historial sin sesión → 401', 'GET', '/historial'),
  sinSesionGri('Validar sin sesión → 401', 'POST', '/validacion', { version: 0, huella_documentos: HUELLA_FALSA }),
  superAdminGri('El SuperAdmin no puede consultar el análisis → 403 FORBIDDEN', 'GET', ''),
  superAdminGri('El SuperAdmin no puede asignar estados → 403 FORBIDDEN', 'PUT', '/codigos/305:2016/estado', { estado: 'OK', version_fila_esperada: 0 }),
  superAdminGri('El SuperAdmin no puede ver el historial → 403 FORBIDDEN', 'GET', '/historial'),
  superAdminGri('El SuperAdmin no puede validar → 403 FORBIDDEN', 'POST', '/validacion', { version: 0, huella_documentos: HUELLA_FALSA }),
  peticion({
    nombre: 'Consultar una empresa/año sin documentos → 200 DOCUMENTACION_INSUFICIENTE', ruta: BASE_GRI, token: 'i_token_admin',
    pruebas: [
      T.estado(200),
      T.prueba('AnalisisResponse completo y vacío', (j, v) => {
        pm.expect(Object.keys(j)).to.have.members([
          'ambiente', 'empresa', 'anio', 'version', 'huella_documentos', 'estado_flujo', 'habilita_reporte', 'version_catalogo', 'documentos',
          'filas', 'advertencias', 'sanciones', 'resumen_sanciones', 'puntaje', 'pendientes', 'validacion',
        ]);
        pm.expect(j.empresa.id).to.equal(v('i_empresa1_id'));
        pm.expect(j.anio).to.equal(Number(v('anio_actual')));
        pm.expect(j.estado_flujo).to.equal('DOCUMENTACION_INSUFICIENTE');
        pm.expect(j.habilita_reporte).to.equal(false);
        pm.expect(j.version).to.equal(0);
        pm.expect(esHex64(j.huella_documentos)).to.equal(true);
        pm.expect([j.documentos, j.filas, j.sanciones]).to.deep.equal([[], [], []]);
        pm.expect(j.validacion).to.equal(null);
      }),
      T.prueba('sin filas no hay puntaje: NO_DISPONIBLE y nunca cero', (j) => {
        pm.expect(j.puntaje.estado).to.equal('NO_DISPONIBLE');
        pm.expect(j.puntaje.valor).to.equal(null);
        pm.expect(j.puntaje.definitivo).to.equal(false);
        pm.expect(j.resumen_sanciones).to.deep.equal({ total: 0, cuantificadas: 0, no_cuantificadas: 0, por_unidad: {} });
        pm.expect(j.pendientes).to.be.an('array').that.is.not.empty;
      }),
      T.guardarCampos({ i_gri_version: 'j.version', i_gri_huella: 'j.huella_documentos' }),
    ],
  }),
  peticion({ nombre: 'Consultar una empresa inexistente → 404 COMPANY_NOT_FOUND', ruta: `/analisis-gri/empresas/${UUID_INEXISTENTE}/anios/{{anio_actual}}`, token: 'i_token_admin', pruebas: [T.error(404, 'COMPANY_NOT_FOUND')] }),
  peticion({ nombre: 'Consultar un año anterior a 2000 → 422', ruta: '/analisis-gri/empresas/{{i_empresa1_id}}/anios/1999', token: 'i_token_admin', pruebas: [T.validacion('anio')] }),
  peticion({ nombre: 'Consultar un año no numérico → 422', ruta: '/analisis-gri/empresas/{{i_empresa1_id}}/anios/abc', token: 'i_token_admin', pruebas: [T.validacion('anio')] }),
  peticion({ nombre: 'Consultar con una empresa que no es UUID → 422', ruta: '/analisis-gri/empresas/no-es-uuid/anios/{{anio_actual}}', token: 'i_token_admin', pruebas: [T.validacion('empresa_id')] }),
  peticion({
    nombre: 'Asignar estado a una identidad que no está en el análisis → 404 ANALYSIS_ROW_NOT_FOUND', metodo: 'PUT', ruta: BASE_GRI + '/codigos/305:2016/estado', token: 'i_token_admin',
    json: { estado: 'OK', version_fila_esperada: 0 }, pruebas: [T.error(404, 'ANALYSIS_ROW_NOT_FOUND')],
  }),
  peticion({
    nombre: 'Asignar estado con una identidad mal formada → 422', metodo: 'PUT', ruta: BASE_GRI + '/codigos/xyz/estado', token: 'i_token_admin',
    json: { estado: 'OK', version_fila_esperada: 0 }, pruebas: [T.validacion('identidad')],
  }),
  peticion({
    nombre: 'Asignar un estado fuera de OK / BAJA_SUSTANCIA / SUB_REPORTADO → 422', metodo: 'PUT', ruta: BASE_GRI + '/codigos/305:2016/estado', token: 'i_token_admin',
    json: { estado: 'CUMPLE', version_fila_esperada: 0 }, pruebas: [T.validacion('estado')],
  }),
  peticion({
    nombre: 'Asignar estado sin version_fila_esperada → 422', metodo: 'PUT', ruta: BASE_GRI + '/codigos/305:2016/estado', token: 'i_token_admin',
    json: { estado: 'OK' }, pruebas: [T.validacion('version_fila_esperada')],
  }),
  peticion({
    nombre: 'Asignar estado con un campo no permitido (el autor sale de la sesión) → 422', metodo: 'PUT', ruta: BASE_GRI + '/codigos/305:2016/estado', token: 'i_token_admin',
    json: { estado: 'OK', version_fila_esperada: 0, usuario_id: '{{i_admin_id}}' }, pruebas: [T.validacion('usuario_id')],
  }),
  peticion({
    nombre: 'Asignar estado en una empresa inexistente → 404 COMPANY_NOT_FOUND', metodo: 'PUT', ruta: `/analisis-gri/empresas/${UUID_INEXISTENTE}/anios/{{anio_actual}}/codigos/305:2016/estado`, token: 'i_token_admin',
    json: { estado: 'OK', version_fila_esperada: 0 }, pruebas: [T.error(404, 'COMPANY_NOT_FOUND')],
  }),
  peticion({
    nombre: 'Historial sin cambios → 200 vacío', ruta: BASE_GRI + '/historial', token: 'i_token_admin',
    pruebas: [T.estado(200), T.prueba('PaginaHistorial vacía', (j) => { pm.expect(j.items).to.deep.equal([]); pm.expect([j.total, j.pagina, j.tamano]).to.deep.equal([0, 1, 20]); })],
  }),
  peticion({ nombre: 'Historial con página 0 → 422', ruta: BASE_GRI + '/historial', consulta: { pagina: '0' }, token: 'i_token_admin', pruebas: [T.validacion('pagina')] }),
  peticion({ nombre: 'Historial con tamaño mayor al máximo → 422', ruta: BASE_GRI + '/historial', consulta: { tamano: '101' }, token: 'i_token_admin', pruebas: [T.validacion('tamano')] }),
  peticion({ nombre: 'Historial con una identidad mal formada → 422', ruta: BASE_GRI + '/historial', consulta: { identidad: 'xyz' }, token: 'i_token_admin', pruebas: [T.validacion('identidad')] }),
  peticion({ nombre: 'Historial de una empresa inexistente → 404 COMPANY_NOT_FOUND', ruta: `/analisis-gri/empresas/${UUID_INEXISTENTE}/anios/{{anio_actual}}/historial`, token: 'i_token_admin', pruebas: [T.error(404, 'COMPANY_NOT_FOUND')] }),
  peticion({
    nombre: 'Validar con una huella que no es la consultada → 409 ANALYSIS_VERSION_CONFLICT (se comprueba antes que los documentos)', metodo: 'POST', ruta: BASE_GRI + '/validacion', token: 'i_token_admin',
    json: { version: 0, huella_documentos: HUELLA_FALSA },
    pruebas: [T.error(409, 'ANALYSIS_VERSION_CONFLICT', (d, v) => {
      pm.expect(d.version_actual).to.equal(Number(v('i_gri_version')));
      pm.expect(d.documentos_cambiaron).to.equal(true);
    })],
  }),
  peticion({
    nombre: 'Validar con la versión y huella consultadas pero sin documentos elegibles → 409 ANALYSIS_NO_ELIGIBLE_DOCUMENTS', metodo: 'POST', ruta: BASE_GRI + '/validacion', token: 'i_token_admin',
    jsonCrudo: '{"version": {{i_gri_version}}, "huella_documentos": "{{i_gri_huella}}"}', pruebas: [T.error(409, 'ANALYSIS_NO_ELIGIBLE_DOCUMENTS')],
  }),
  peticion({ nombre: 'Validar con una huella de longitud inválida → 422', metodo: 'POST', ruta: BASE_GRI + '/validacion', token: 'i_token_admin', json: { version: 0, huella_documentos: 'corta' }, pruebas: [T.validacion('huella_documentos')] }),
  peticion({ nombre: 'Validar con una versión negativa → 422', metodo: 'POST', ruta: BASE_GRI + '/validacion', token: 'i_token_admin', json: { version: -1, huella_documentos: HUELLA_FALSA }, pruebas: [T.validacion('version')] }),
  peticion({ nombre: 'Validar con un campo no permitido → 422', metodo: 'POST', ruta: BASE_GRI + '/validacion', token: 'i_token_admin', json: { version: 0, huella_documentos: HUELLA_FALSA, forzar: true }, pruebas: [T.validacion('forzar')] }),
  peticion({ nombre: 'Validar una empresa inexistente → 404 COMPANY_NOT_FOUND', metodo: 'POST', ruta: `/analisis-gri/empresas/${UUID_INEXISTENTE}/anios/{{anio_actual}}/validacion`, token: 'i_token_admin', json: { version: 0, huella_documentos: HUELLA_FALSA }, pruebas: [T.error(404, 'COMPANY_NOT_FOUND')] }),
]);

const cierre = carpeta('99 Cierre de los datos sintéticos', 'Deja las cuentas sintéticas deshabilitadas y las empresas inactivas (no hay endpoints de borrado; ver README «Datos que quedan»).', [
  peticion({ nombre: 'Deshabilitar el Administrador sintético 1', metodo: 'PATCH', ruta: '/usuarios/{{i_admin_id}}/deshabilitar', token: 'i_token_super', pruebas: [T.estado(200), T.prueba('deshabilitado', (j) => { pm.expect(j.habilitado).to.equal(false); })] }),
  peticion({ nombre: 'Deshabilitar el Administrador sintético 2', metodo: 'PATCH', ruta: '/usuarios/{{i_admin2_id}}/deshabilitar', token: 'i_token_super', pruebas: [T.estado(200), T.prueba('deshabilitado', (j) => { pm.expect(j.habilitado).to.equal(false); })] }),
  peticion({ nombre: 'Desactivar la empresa sintética 1', metodo: 'PATCH', ruta: '/empresas/{{i_empresa1_id}}/desactivar', token: 'i_token_super', pruebas: [T.estado(200), T.prueba('inactiva', (j) => { pm.expect(j.activa).to.equal(false); })] }),
  peticion({ nombre: 'Desactivar la empresa sintética 2', metodo: 'PATCH', ruta: '/empresas/{{i_empresa2_id}}/desactivar', token: 'i_token_super', pruebas: [T.estado(200), T.prueba('inactiva', (j) => { pm.expect(j.activa).to.equal(false); })] }),
]);

export const coleccionIntegracion = coleccion({
  nombre: 'Igualab · Integración HTTP del backend',
  descripcion:
    'Pruebas de integración de los endpoints implementados (autenticación, usuarios, empresas, documentos y análisis GRI) contra un backend desplegado. ' +
    'Cada ejecución crea sus propias cuentas y empresas sintéticas (run_id) y no ingiere documentos ni usa OCI. Ver pruebas-http/README.md.',
  variables: { base_url: 'http://localhost:8000' },
  items: [infraestructura, autenticacion, usuarios, empresas, documentos, analisisGri, cierre],
});
