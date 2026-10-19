/**
 * Colección E2E por HTTP: los siete flujos del backend sobre el sistema DESPLEGADO (sin dobles ni mocks).
 *
 *  A. Administración y revocación de acceso.
 *  B. Ingesta y validación GRI (ingiere un Markdown sintético: usa OCI real con una muestra mínima).
 *  C. Rechazo de duplicado por contenido (usa el documento completado de B).
 *  D. Rechazo de ingesta para una empresa inactiva.
 *  E. Reportes de prospección (PDF)  ·  F. Asistente de IA (RAG)  ·  G. Auditoría funcional (los tres usan los datos de B).
 *  Z. Cierre de los datos sintéticos de B (no es un flujo funcional: ver abajo).
 *
 * Cada flujo prepara sus propios datos con el identificador de ejecución `run_id`. A y D son independientes; C, E, F y G necesitan
 * a B (REQUISITOS_E2E). Todo ID, token, versión y huella sale de la respuesta anterior de ESTA ejecución.
 * No hay endpoints de borrado: ver README «Datos que quedan». Nunca se transfiere el rol SuperAdmin.
 *
 * CIERRE DE B (H07). La empresa y el Administrador de B se desactivan/deshabilitan en la carpeta Z, que va SIEMPRE al final: antes lo
 * hacía C, y la sesión revocada del Administrador (401) y la empresa inactiva (400) rompían a E, F y G. El ejecutor resuelve la
 * selección con estos dos mapas, que viajan en el JSON como variables de la colección: exige los requisitos antes de la primera
 * petición y añade Z al final siempre que B forme parte de la selección.
 */
import { T, carpeta, coleccion, peticion } from './dsl.mjs';

/** Carpeta (por letra) → carpetas cuyos datos necesita. Una selección sin ellas falla antes de enviar nada. */
export const REQUISITOS_E2E = { C: ['B'], E: ['B'], F: ['B'], G: ['B'], Z: ['B'] };
/** Carpeta (por letra) → carpeta de cierre que el ejecutor añade AL FINAL cuando la primera está seleccionada. */
export const CIERRES_E2E = { B: 'Z' };

const SOLO_HTTP =
  'Evidencia solo por HTTP: los contratos no permiten demostrar la ausencia física de objetos en MinIO ni de vectores en pgvector; ' +
  'esta prueba NO afirma haberla verificado y no hay endpoints internos para ello.';

const loginSuper = (nombre, variable) =>
  peticion({
    nombre, metodo: 'POST', ruta: '/auth/login', json: { correo: '{{superadmin_correo}}', password: '{{superadmin_password}}' },
    pruebas: [
      // Credenciales vigentes incorrectas: se detiene TODA la corrida tras el primer intento (evita bloquear la cuenta, RNF-031).
      "if (pm.response.code === 401 || pm.response.code === 423) { pm.execution.setNextRequest(null); }",
      T.estado(200),
      T.prueba('LoginResponse del SuperAdmin: JWT, bearer y rol SUPERADMIN', (j) => {
        pm.expect(Object.keys(j)).to.have.members(['access_token', 'token_type', 'rol', 'nombre']);
        pm.expect(esJwt(j.access_token)).to.equal(true);
        pm.expect(j.token_type).to.equal('bearer');
        pm.expect(j.rol).to.equal('superadmin');
      }),
      T.guardarCampos({ [variable]: 'j.access_token' }),
    ],
  });

const crearAdministrador = (nombre, prefijo, etiqueta) =>
  peticion({
    nombre, metodo: 'POST', ruta: '/usuarios', token: `${prefijo}_token_super`,
    json: { nombre: `E2E {{run_id}} Administrador ${etiqueta}`, correo: `e2e-{{run_id}}-${prefijo}@igualab-e2e.example.com`, password: `{{pwd_${prefijo}}}` },
    pruebas: [
      T.estado(201),
      T.prueba('Administrador habilitado, con rol ADMINISTRADOR y sin datos de contraseña', (j, v) => {
        pm.expect(esUuid(j.id)).to.equal(true);
        pm.expect(j.rol).to.equal('administrador');
        pm.expect(j.habilitado).to.equal(true);
        pm.expect(j.nombre).to.include(v('run_id'));
        pm.expect(sinSecretos(j)).to.equal(true);
      }),
      T.guardarCampos({ [`${prefijo}_admin_id`]: 'j.id', [`${prefijo}_admin_correo`]: 'j.correo', [`${prefijo}_admin_nombre`]: 'j.nombre' }),
    ],
  });

const loginAdministrador = (nombre, prefijo) =>
  peticion({
    nombre, metodo: 'POST', ruta: '/auth/login', json: { correo: `{{${prefijo}_admin_correo}}`, password: `{{pwd_${prefijo}}}` },
    pruebas: [
      T.estado(200),
      T.prueba('el Administrador de prueba se autentica con rol ADMINISTRADOR', (j) => {
        pm.expect(j.rol).to.equal('administrador');
        pm.expect(esJwt(j.access_token)).to.equal(true);
      }),
      T.guardarCampos({ [`${prefijo}_token_admin`]: 'j.access_token' }),
    ],
  });

// ---- A. Administración y revocación de acceso ---------------------------------------------------------------------------

const flujoA = carpeta('A. Administración y revocación de acceso',
  'SuperAdmin crea un Administrador → éste se autentica y accede → el SuperAdmin lo deshabilita → el token anterior pierde acceso. Usa solo la cuenta que crea esta prueba y no transfiere el rol SuperAdmin.',
  [
    loginSuper('A1 Autenticar SuperAdmin', 'a_token_super'),
    peticion({
      nombre: 'A2 Crear Administrador de prueba', metodo: 'POST', ruta: '/usuarios', token: 'a_token_super',
      json: { nombre: 'E2E {{run_id}} Administrador A', correo: 'e2e-{{run_id}}-a@igualab-e2e.example.com', password: '{{pwd_a}}' },
      pruebas: [
        T.estado(201),
        T.prueba('cuenta habilitada, ADMINISTRADOR, creada ahora y sin datos de contraseña', (j, v) => {
          pm.expect(Object.keys(j)).to.have.members(['id', 'nombre', 'correo', 'rol', 'habilitado', 'creado_en']);
          pm.expect(esUuid(j.id)).to.equal(true);
          pm.expect(j.rol).to.equal('administrador');
          pm.expect(j.habilitado).to.equal(true);
          pm.expect(j.correo).to.equal('e2e-' + v('run_id') + '-a@igualab-e2e.example.com');
          pm.expect(Math.abs(Date.now() - Date.parse(j.creado_en)) < 10 * 60 * 1000, 'creada en los últimos 10 minutos').to.equal(true);
          pm.expect(sinSecretos(j)).to.equal(true);
        }),
        T.guardar((j, set) => { set('a_admin_id', j.id); set('a_admin_correo', j.correo); }),
      ],
    }),
    peticion({
      nombre: 'A3 Autenticar al Administrador de prueba', metodo: 'POST', ruta: '/auth/login', json: { correo: '{{a_admin_correo}}', password: '{{pwd_a}}' },
      pruebas: [
        T.estado(200),
        T.prueba('rol ADMINISTRADOR y token JWT', (j) => { pm.expect(j.rol).to.equal('administrador'); pm.expect(esJwt(j.access_token)).to.equal(true); }),
        T.guardar((j, set) => set('a_token_admin', j.access_token)),
      ],
    }),
    peticion({
      nombre: 'A4 Acceso autorizado del Administrador (empresas activas)', ruta: '/empresas', consulta: { solo_activas: 'true' }, token: 'a_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('lista de empresas activas', (j) => { pm.expect(j).to.be.an('array'); j.forEach((e) => pm.expect(e.activa).to.equal(true)); }),
      ],
    }),
    peticion({
      nombre: 'A5 El rol sigue acotado: el Administrador no accede a la gestión de usuarios', ruta: '/usuarios', token: 'a_token_admin',
      pruebas: [T.prohibido()],
    }),
    peticion({
      nombre: 'A6 El SuperAdmin deshabilita al Administrador de prueba', metodo: 'PATCH', ruta: '/usuarios/{{a_admin_id}}/deshabilitar', token: 'a_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('cuenta deshabilitada', (j, v) => { pm.expect(j.id).to.equal(v('a_admin_id')); pm.expect(j.habilitado).to.equal(false); pm.expect(j.rol).to.equal('administrador'); }),
      ],
    }),
    peticion({
      nombre: 'A7 Repetir la petición protegida con el token anterior → 401 (perdió el acceso)', ruta: '/empresas', consulta: { solo_activas: 'true' }, token: 'a_token_admin',
      descripcion: 'Mismo token y misma petición que A4: antes 200, ahora 401 INVALID_SESSION (la sesión se revoca al deshabilitar).',
      pruebas: [T.sinSesion()],
    }),
    peticion({
      nombre: 'A8 La cuenta deshabilitada tampoco puede autenticarse de nuevo → 403', metodo: 'POST', ruta: '/auth/login', json: { correo: '{{a_admin_correo}}', password: '{{pwd_a}}' },
      pruebas: [T.error(403, 'ACCOUNT_DISABLED')],
    }),
    peticion({
      nombre: 'A9 El SuperAdmin conserva su rol y es el único (no se transfirió nada)', ruta: '/usuarios', token: 'a_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('un solo SuperAdmin: el de esta ejecución; la cuenta de prueba figura deshabilitada', (j, v) => {
          const supers = j.filter((u) => u.rol === 'superadmin');
          pm.expect(supers).to.have.length(1);
          pm.expect(supers[0].correo).to.equal(String(v('superadmin_correo')).toLowerCase());
          pm.expect(supers[0].habilitado).to.equal(true);
          pm.expect(j.find((u) => u.id === v('a_admin_id')).habilitado).to.equal(false);
        }),
      ],
    }),
  ]);

// ---- B. Ingesta y validación GRI -----------------------------------------------------------------------------------------

const ESTADOS_B = ['OK', 'BAJA_SUSTANCIA', 'SUB_REPORTADO'];

/** Función del sandbox: puntaje esperado calculado desde los estados REALMENTE asignados (devueltos por cada PUT). */
const puntajeEsperado = (v) => {
  const puntos = { OK: 100, BAJA_SUSTANCIA: 50, SUB_REPORTADO: 0 };
  const estados = [v('b_estado_1'), v('b_estado_2'), v('b_estado_3')];
  const suma = estados.reduce((total, e) => total + puntos[e], 0);
  return { suma, valor: suma / estados.length, filas: estados.length };
};

const AYUDANTE_PUNTAJE = `const puntajeEsperado = ${puntajeEsperado.toString()};`;

const asignarEstado = (indice, estado) =>
  peticion({
    nombre: `B${10 + indice} Asignar «${estado}» a la identidad ${indice} devuelta por el análisis`, metodo: 'PUT',
    ruta: `/analisis-gri/empresas/{{b_empresa_id}}/anios/{{anio_b}}/codigos/{{b_id_${indice}}}/estado`, token: 'b_token_admin',
    jsonCrudo: `{"estado": "${estado}", "version_fila_esperada": {{b_vf_${indice}}}}`,
    pruebas: [
      T.estado(200),
      T.prueba('AsignarEstadoResponse: cambio nuevo, autor = sesión del Administrador, versión de análisis incremental', (j, v, a) => {
        pm.expect(j.identidad).to.equal(v('b_id_' + a.indice));
        pm.expect(j.cambio).to.equal(true);
        pm.expect(j.estado_anterior).to.equal(null);
        pm.expect(j.estado).to.equal(a.estado);
        pm.expect(j.version_fila).to.equal(1);
        pm.expect(j.version_analisis).to.equal(a.indice);
        pm.expect(j.asignado_por.id, 'el autor sale de la sesión, no del cuerpo').to.equal(v('b_admin_id'));
        pm.expect(j.asignado_por.nombre).to.equal(v('b_admin_nombre'));
        pm.expect(esFecha(j.asignado_en)).to.equal(true);
        pm.expect(j.validacion_previa_invalidada).to.equal(false);
      }, { indice, estado }),
      T.guardarCampos({ [`b_estado_${indice}`]: 'j.estado' }),
    ],
  });

const flujoB = carpeta('B. Ingesta y validación GRI',
  'SuperAdmin crea la empresa e ingiere un Markdown sintético (OCI real, muestra mínima) → un Administrador consulta el análisis, asigna estados a las identidades devueltas, consulta versión y huella actuales y valida. B prepara el documento que usa C.',
  [
    loginSuper('B1 Autenticar SuperAdmin', 'b_token_super'),
    peticion({
      nombre: 'B2 Crear una empresa activa', metodo: 'POST', ruta: '/empresas', token: 'b_token_super', json: { nombre: 'E2E {{run_id}} Empresa B', sector: 'ENERGIA' },
      pruebas: [
        T.estado(201),
        T.prueba('empresa activa del sector ENERGIA', (j) => { pm.expect(j.activa).to.equal(true); pm.expect(j.sector).to.equal('ENERGIA'); pm.expect(esUuid(j.id)).to.equal(true); }),
        T.guardar((j, set) => set('b_empresa_id', j.id)),
      ],
    }),
    peticion({
      nombre: 'B3 Crear la operación de ingesta', metodo: 'POST', ruta: '/documentos/operaciones', token: 'b_token_super',
      pruebas: [
        T.estado(201),
        T.prueba('operación CREADA con sus rutas', (j) => { pm.expect(j.estado).to.equal('CREADA'); pm.expect(esUuid(j.operacion_id)).to.equal(true); }),
        T.guardar((j, set) => set('b_operacion_id', j.operacion_id)),
      ],
    }),
    peticion({
      nombre: 'B4 Ingerir el Markdown sintético → 201 COMPLETADO y publicado', metodo: 'POST', ruta: '/documentos/operaciones/{{b_operacion_id}}/ingesta', token: 'b_token_super',
      descripcion: 'Espera síncronamente hasta que el documento queda COMPLETADO y PUBLICADO (usa OCI real para los embeddings de una muestra mínima). No se reintenta.',
      formulario: [
        { key: 'archivo', src: 'muestras/b-reporte.md' }, { key: 'empresa_id', value: '{{b_empresa_id}}' },
        { key: 'anio', value: '{{anio_b}}' }, { key: 'tipo_documento', value: 'REPORTE_SOSTENIBILIDAD_GRI' },
      ],
      pruebas: [
        T.estado(201),
        T.prueba('ResultadoIngestaResponse: COMPLETADO, exitosa, terminal, con documento_id y fragmentos publicados', (j, v) => {
          pm.expect(j.estado).to.equal('COMPLETADO');
          pm.expect(j.exitosa).to.equal(true);
          pm.expect(j.terminal).to.equal(true);
          pm.expect(j.operacion_id).to.equal(v('b_operacion_id'));
          pm.expect(esUuid(j.documento_id)).to.equal(true);
          pm.expect(j.etapa).to.equal('FINALIZADO');
          pm.expect(j.error).to.equal(null);
          pm.expect(j.publicacion_reintentable).to.equal(false);
          pm.expect(j.fragmentos).to.be.above(0);
          pm.expect(j.fragmentos_procesados).to.equal(j.fragmentos_total);
        }),
        T.prueba('el análisis encontró referencias GRI catalogadas (CON_HALLAZGOS) y ninguna sanción', (j) => {
          pm.expect(j.resultado_analisis).to.equal('CON_HALLAZGOS');
          pm.expect(j.motivos).to.include('referencias_gri_catalogadas');
          pm.expect(j.motivos).to.not.include('sanciones_economicas');
        }),
        T.guardar((j, set) => { set('b_documento_id', j.documento_id); set('b_fragmentos', j.fragmentos); }),
      ],
    }),
    peticion({
      nombre: 'B5 Consultar la operación → COMPLETADO (publicación confirmada)', ruta: '/documentos/operaciones/{{b_operacion_id}}', token: 'b_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('la operación confirma el éxito y el mismo documento', (j, v) => {
          pm.expect(j.estado).to.equal('COMPLETADO');
          pm.expect(j.exitosa).to.equal(true);
          pm.expect(j.terminal).to.equal(true);
          pm.expect(j.documento_id).to.equal(v('b_documento_id'));
          pm.expect(j.fragmentos_total).to.equal(Number(v('b_fragmentos')));
          pm.expect(j.resultado_analisis).to.equal('CON_HALLAZGOS');
        }),
      ],
    }),
    peticion({
      nombre: 'B6 Detalle del documento: disponible para RAG, SHA-256 y citas exactas', ruta: '/documentos/{{b_documento_id}}', token: 'b_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('documento COMPLETADO y disponible para RAG, con la procedencia declarada', (j, v) => {
          pm.expect(j.id).to.equal(v('b_documento_id'));
          pm.expect(j.operacion_id).to.equal(v('b_operacion_id'));
          pm.expect(j.estado).to.equal('COMPLETADO');
          pm.expect(j.disponible_para_rag).to.equal(true);
          pm.expect(j.empresa_id).to.equal(v('b_empresa_id'));
          pm.expect(j.sector).to.equal('ENERGIA');
          pm.expect(j.tipo).to.equal('REPORTE_SOSTENIBILIDAD_GRI');
          pm.expect(j.anio).to.equal(Number(v('anio_b')));
          pm.expect(j.nombre_archivo).to.equal(v('nombre_b'));
          pm.expect(j.resultado_analisis).to.equal('CON_HALLAZGOS');
          pm.expect(j.cargado_por).to.be.a('string');
        }),
        T.prueba('el SHA-256 y el tamaño guardados son los de los bytes enviados', (j, v) => {
          pm.expect(j.sha256).to.equal(v('sha256_b'));
          pm.expect(j.tamano_bytes).to.equal(Number(v('tamano_b')));
        }),
        T.prueba('cada mención GRI guardada cita un trozo EXACTO del texto enviado', (j, v) => {
          const texto = v('texto_b');
          const grupos = j.analisis.gri;
          pm.expect(grupos.map((g) => g.codigo + ':' + g.edicion).sort()).to.deep.equal(['302:2016', '303:2018', '305:2016']);
          pm.expect(j.analisis.sanciones).to.deep.equal([]);
          grupos.forEach((g) => {
            pm.expect(g.catalogado).to.equal(true);
            pm.expect(g.menciones.length).to.be.above(0);
            g.menciones.forEach((m) => {
              pm.expect(texto.slice(m.inicio, m.fin)).to.equal(m.referencia_original);
              pm.expect(texto.slice(m.cita_inicio, m.cita_fin)).to.equal(m.cita);
              pm.expect(m.cita).to.include(m.referencia_original);
            });
          });
        }),
      ],
    }),
    peticion({
      nombre: 'B7 El listado de la empresa contiene ese único documento', ruta: '/documentos', consulta: { empresa_id: '{{b_empresa_id}}' }, token: 'b_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('un solo documento COMPLETADO, el de B4', (j, v) => {
          pm.expect(j.total).to.equal(1);
          pm.expect(j.items).to.have.length(1);
          pm.expect(j.items[0].id).to.equal(v('b_documento_id'));
          pm.expect(j.items[0].estado).to.equal('COMPLETADO');
          pm.expect(j.items[0].disponible_para_rag).to.equal(true);
          pm.expect(j.items[0].sha256).to.equal(v('sha256_b'));
        }),
      ],
    }),
    crearAdministrador('B8 Crear un Administrador de prueba habilitado (B)', 'b', 'B'),
    loginAdministrador('B9 Autenticar al Administrador de prueba', 'b'),
    peticion({
      nombre: 'B10 Consultar el análisis de la empresa/año: identidades, evidencias y procedencia', ruta: '/analisis-gri/empresas/{{b_empresa_id}}/anios/{{anio_b}}', token: 'b_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('análisis PENDIENTE_ESTADOS de la empresa y el año ingeridos, con el documento elegible', (j, v) => {
          pm.expect(j.empresa.id).to.equal(v('b_empresa_id'));
          pm.expect(j.anio).to.equal(Number(v('anio_b')));
          pm.expect(j.estado_flujo).to.equal('PENDIENTE_ESTADOS');
          pm.expect(j.habilita_reporte).to.equal(false);
          pm.expect(j.version).to.equal(0);
          pm.expect(j.documentos).to.have.length(1);
          pm.expect(j.documentos[0].documento_id).to.equal(v('b_documento_id'));
          pm.expect(j.documentos[0].nombre_archivo).to.equal(v('nombre_b'));
          pm.expect(j.documentos[0].resultado_analisis).to.equal('CON_HALLAZGOS');
          pm.expect(j.sanciones).to.deep.equal([]);
        }),
        T.prueba('tres filas GRI sin estado, de edición explícita y sin revisión pendiente', (j) => {
          pm.expect(j.filas.map((f) => f.identidad)).to.deep.equal(['302:2016', '303:2018', '305:2016']);
          j.filas.forEach((f) => {
            pm.expect(f.estado).to.equal(null);
            pm.expect(f.version_fila).to.equal(0);
            pm.expect(f.identidad_tipo).to.equal('EDICION_RESUELTA');
            pm.expect(f.requiere_revision).to.equal(false);
          });
          pm.expect(j.puntaje.estado).to.equal('PENDIENTE_ESTADOS');
          pm.expect(j.puntaje.valor).to.equal(null);
        }),
        T.prueba('cada evidencia conserva su procedencia (documento) y su cita exacta', (j, v) => {
          const texto = v('texto_b');
          j.filas.forEach((f) => {
            pm.expect(f.evidencias.length).to.be.above(0);
            f.evidencias.forEach((e) => {
              pm.expect(e.documento_id).to.equal(v('b_documento_id'));
              pm.expect(e.documento_nombre).to.equal(v('nombre_b'));
              pm.expect(texto.slice(e.inicio, e.fin)).to.equal(e.referencia_original);
              pm.expect(texto.slice(e.cita_inicio, e.cita_fin)).to.equal(e.cita);
              pm.expect(e.seccion).to.be.a('string').and.not.empty;
            });
          });
        }),
        T.guardar((j, set) => {
          j.filas.forEach((f, i) => { set('b_id_' + (i + 1), f.identidad); set('b_vf_' + (i + 1), f.version_fila); });
          set('b_huella_inicial', j.huella_documentos);
        }),
      ],
    }),
    ...ESTADOS_B.map((estado, i) => asignarEstado(i + 1, estado)),
    peticion({
      nombre: 'B14 Consultar de nuevo: versión y huella_documentos actuales y puntaje calculado', ayudantes: [AYUDANTE_PUNTAJE], ruta: '/analisis-gri/empresas/{{b_empresa_id}}/anios/{{anio_b}}', token: 'b_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('LISTO_PARA_VALIDAR con versión 3 y la huella de los mismos documentos', (j, v) => {
          pm.expect(j.estado_flujo).to.equal('LISTO_PARA_VALIDAR');
          pm.expect(j.version).to.equal(3);
          pm.expect(j.huella_documentos).to.equal(v('b_huella_inicial'));
          pm.expect(j.validacion).to.equal(null);
          pm.expect(j.habilita_reporte).to.equal(false);
          j.filas.forEach((f, i) => {
            pm.expect(f.estado).to.equal(v('b_estado_' + (i + 1)));
            pm.expect(f.version_fila).to.equal(1);
            pm.expect(f.asignado_por.id).to.equal(v('b_admin_id'));
          });
        }),
        T.prueba('puntaje CALCULADO_SIN_VALIDAR = promedio de los puntos de los estados realmente asignados', (j, v) => {
          const esperado = puntajeEsperado(v);
          pm.expect(j.puntaje.estado).to.equal('CALCULADO_SIN_VALIDAR');
          pm.expect(j.puntaje.definitivo).to.equal(false);
          pm.expect(j.puntaje.suma_puntos).to.equal(esperado.suma);
          pm.expect(j.puntaje.filas_contadas).to.equal(esperado.filas);
          pm.expect(j.puntaje.valor).to.be.closeTo(esperado.valor, 1e-9);
        }),
        T.guardar((j, set) => { set('b_version', j.version); set('b_huella', j.huella_documentos); }),
      ],
    }),
    peticion({
      nombre: 'B15 Validar el análisis con la versión y huella consultadas → 201', ayudantes: [AYUDANTE_PUNTAJE], metodo: 'POST', ruta: '/analisis-gri/empresas/{{b_empresa_id}}/anios/{{anio_b}}/validacion', token: 'b_token_admin',
      jsonCrudo: '{"version": {{b_version}}, "huella_documentos": "{{b_huella}}"}',
      pruebas: [
        T.estado(201),
        T.prueba('validación vigente #1 hecha por el Administrador sobre esa versión y huella', (j, v) => {
          pm.expect(j.estado_flujo).to.equal('VALIDADO');
          pm.expect(j.habilita_reporte).to.equal(true);
          pm.expect(j.validacion.numero).to.equal(1);
          pm.expect(j.validacion.vigente).to.equal(true);
          pm.expect(j.validacion.motivos_no_vigente).to.deep.equal([]);
          pm.expect(j.validacion.version_estados).to.equal(Number(v('b_version')));
          pm.expect(j.validacion.huella_documentos).to.equal(v('b_huella'));
          pm.expect(j.validacion.validado_por.id).to.equal(v('b_admin_id'));
          pm.expect(j.validacion.documentos_validados).to.equal(1);
          pm.expect(j.validacion.filas_validadas).to.equal(3);
          pm.expect(esFecha(j.validacion.validado_en)).to.equal(true);
        }),
        T.prueba('puntaje VALIDADO y definitivo, igual al esperado de los estados asignados', (j, v) => {
          const esperado = puntajeEsperado(v);
          pm.expect(j.puntaje.estado).to.equal('VALIDADO');
          pm.expect(j.puntaje.definitivo).to.equal(true);
          pm.expect(j.puntaje.suma_puntos).to.equal(esperado.suma);
          pm.expect(j.puntaje.valor).to.be.closeTo(esperado.valor, 1e-9);
          pm.expect(j.validacion.puntaje_suma).to.equal(esperado.suma);
          pm.expect(j.validacion.puntaje_filas).to.equal(esperado.filas);
        }),
      ],
    }),
    peticion({
      nombre: 'B16 Consultar el historial: tres cambios con su autor y su versión', ruta: '/analisis-gri/empresas/{{b_empresa_id}}/anios/{{anio_b}}/historial', token: 'b_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('un cambio por identidad: estado anterior nulo, estado nuevo, autor y versión de análisis 1..3', (j, v) => {
          pm.expect(j.total).to.equal(3);
          pm.expect(j.items).to.have.length(3);
          pm.expect(j.items.map((i) => i.version_analisis).sort()).to.deep.equal([1, 2, 3]);
          pm.expect(j.items.map((i) => i.identidad).sort()).to.deep.equal([v('b_id_1'), v('b_id_2'), v('b_id_3')].sort());
          j.items.forEach((i) => {
            const n = [v('b_id_1'), v('b_id_2'), v('b_id_3')].indexOf(i.identidad) + 1;
            pm.expect(i.estado_anterior).to.equal(null);
            pm.expect(i.estado_nuevo).to.equal(v('b_estado_' + n));
            pm.expect(i.usuario.id).to.equal(v('b_admin_id'));
            pm.expect(esFecha(i.cambiado_en)).to.equal(true);
          });
        }),
      ],
    }),
    peticion({
      nombre: 'B17 Consultar tras validar: la validación sigue vigente', ruta: '/analisis-gri/empresas/{{b_empresa_id}}/anios/{{anio_b}}', token: 'b_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('VALIDADO, vigente y con puntaje definitivo', (j, v) => {
          pm.expect(j.estado_flujo).to.equal('VALIDADO');
          pm.expect(j.habilita_reporte).to.equal(true);
          pm.expect(j.validacion.vigente).to.equal(true);
          pm.expect(j.validacion.version_estados).to.equal(j.version);
          pm.expect(j.puntaje.definitivo).to.equal(true);
        }),
      ],
    }),
    peticion({
      nombre: 'B18 Conflicto: reasignar con la versión de fila vieja → 409 ANALYSIS_ROW_VERSION_CONFLICT (sin cambios)', metodo: 'PUT',
      ruta: '/analisis-gri/empresas/{{b_empresa_id}}/anios/{{anio_b}}/codigos/{{b_id_1}}/estado', token: 'b_token_admin',
      json: { estado: 'SUB_REPORTADO', version_fila_esperada: 0 }, pruebas: [T.error(409, 'ANALYSIS_ROW_VERSION_CONFLICT')],
    }),
    peticion({
      nombre: 'B19 Conflicto: validar de nuevo la misma versión → 409 ANALYSIS_ALREADY_VALIDATED', metodo: 'POST', ruta: '/analisis-gri/empresas/{{b_empresa_id}}/anios/{{anio_b}}/validacion', token: 'b_token_admin',
      jsonCrudo: '{"version": {{b_version}}, "huella_documentos": "{{b_huella}}"}', pruebas: [T.error(409, 'ANALYSIS_ALREADY_VALIDATED')],
    }),
  ]);

// ---- C. Rechazo de duplicado por contenido ------------------------------------------------------------------------------

const flujoC = carpeta('C. Rechazo de duplicado por contenido',
  'Reenvía EXACTAMENTE los mismos bytes del documento de B con otro nombre de archivo y otro año (así solo coincide el SHA-256, no empresa/año/tipo). Requiere a B: ejecutar `--carpeta B --carpeta C`. No cierra los datos de B: lo hace Z, al final.',
  [
    peticion({
      nombre: 'C1 Crear otra operación de ingesta', metodo: 'POST', ruta: '/documentos/operaciones', token: 'b_token_super',
      descripcion: 'Reutiliza la sesión SuperAdmin de B (misma corrida B→C).',
      pruebas: [
        T.prueba('hay un documento de B contra el que duplicar', (j, v) => { pm.expect(esUuid(v('b_documento_id')), 'B debe haber completado un documento (ejecutar B antes que C)').to.equal(true); }),
        T.estado(201),
        T.guardar((j, set) => set('c_operacion_id', j.operacion_id)),
      ],
    }),
    peticion({
      nombre: 'C2 Subir los mismos bytes con otro nombre y otro año → 409 DOCUMENT_ALREADY_INGESTED por sha256', metodo: 'POST', ruta: '/documentos/operaciones/{{c_operacion_id}}/ingesta', token: 'b_token_super',
      descripcion: 'Archivo muestras/c-copia.md: bytes idénticos a muestras/b-reporte.md (el ejecutor verifica el SHA-256 de ambos antes de empezar). Año distinto → combinación empresa/año/tipo libre.',
      formulario: [
        { key: 'archivo', src: 'muestras/c-copia.md' }, { key: 'empresa_id', value: '{{b_empresa_id}}' },
        { key: 'anio', value: '{{anio_c}}' }, { key: 'tipo_documento', value: 'REPORTE_SOSTENIBILIDAD_GRI' },
      ],
      pruebas: [
        T.error(409, 'DOCUMENT_ALREADY_INGESTED', (d, v) => {
          pm.expect(d.criterio, 'solo coincide el contenido (SHA-256), no empresa/año/tipo').to.deep.equal(['sha256']);
          pm.expect(d.documento_id).to.equal(v('b_documento_id'));
          pm.expect(esFecha(d.cargado_en)).to.equal(true);
          pm.expect(d.cuenta).to.equal(String(v('superadmin_correo')).toLowerCase());
        }),
        T.prueba('el rechazo no devuelve una ingesta exitosa ni contenido del documento', (j) => {
          pm.expect(j).to.not.have.property('exitosa');
          pm.expect(j).to.not.have.property('estado');
          pm.expect(JSON.stringify(j)).to.not.include('Reporte de sostenibilidad sintético');
        }),
      ],
    }),
    peticion({
      nombre: 'C3 La operación del intento refleja el rechazo (RECHAZADA, sin documento)', ruta: '/documentos/operaciones/{{c_operacion_id}}', token: 'b_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('RECHAZADA, terminal, NO exitosa, sin documento ni análisis y con el código del duplicado', (j) => {
          pm.expect(j.estado).to.equal('RECHAZADA');
          pm.expect(j.terminal).to.equal(true);
          pm.expect(j.exitosa).to.equal(false);
          pm.expect(j.documento_id).to.equal(null);
          pm.expect(j.resultado_analisis).to.equal(null);
          pm.expect(j.fragmentos_procesados).to.equal(0);
          pm.expect(j.error.code).to.equal('DOCUMENT_ALREADY_INGESTED');
        }),
      ],
    }),
    peticion({
      nombre: 'C4 El documento original sigue COMPLETADO y disponible', ruta: '/documentos/{{b_documento_id}}', token: 'b_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('mismo documento, estado, año y SHA-256; disponible para RAG', (j, v) => {
          pm.expect(j.id).to.equal(v('b_documento_id'));
          pm.expect(j.estado).to.equal('COMPLETADO');
          pm.expect(j.disponible_para_rag).to.equal(true);
          pm.expect(j.anio).to.equal(Number(v('anio_b')));
          pm.expect(j.sha256).to.equal(v('sha256_b'));
          pm.expect(j.resultado_analisis).to.equal('CON_HALLAZGOS');
        }),
      ],
    }),
    peticion({
      nombre: 'C5 El listado de la empresa sigue teniendo UN documento (el intento no creó otro)', ruta: '/documentos', consulta: { empresa_id: '{{b_empresa_id}}' }, token: 'b_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('un único documento, el original, y ninguno del año del intento duplicado', (j, v) => {
          pm.expect(j.total).to.equal(1);
          pm.expect(j.items.map((d) => d.id)).to.deep.equal([v('b_documento_id')]);
          pm.expect(j.items.some((d) => d.anio === Number(v('anio_c')))).to.equal(false);
        }),
      ],
    }),
    peticion({
      nombre: 'C6 Tampoco hay documentos en el año del intento (ni COMPLETADO ni de otro estado)', ruta: '/documentos', consulta: { empresa_id: '{{b_empresa_id}}', anio: '{{anio_c}}' }, token: 'b_token_super',
      pruebas: [T.estado(200), T.prueba('cero documentos', (j) => { pm.expect(j.total).to.equal(0); pm.expect(j.items).to.deep.equal([]); })],
    }),
  ]);

// ---- D. Rechazo de ingesta para una empresa inactiva ---------------------------------------------------------------------

const flujoD = carpeta('D. Rechazo de ingesta para una empresa inactiva',
  `Crea una empresa, la desactiva e intenta ingerir un Markdown válido y exclusivo de este flujo: el rechazo debe deberse solo a la empresa inactiva. ${SOLO_HTTP}`,
  [
    loginSuper('D1 Autenticar SuperAdmin', 'd_token_super'),
    peticion({
      nombre: 'D2 Crear otra empresa de prueba (activa)', metodo: 'POST', ruta: '/empresas', token: 'd_token_super', json: { nombre: 'E2E {{run_id}} Empresa D', sector: 'PETROLEO' },
      pruebas: [T.estado(201), T.prueba('activa', (j) => { pm.expect(j.activa).to.equal(true); }), T.guardar((j, set) => set('d_empresa_id', j.id))],
    }),
    peticion({
      nombre: 'D3 Desactivar la empresa por su endpoint', metodo: 'PATCH', ruta: '/empresas/{{d_empresa_id}}/desactivar', token: 'd_token_super',
      pruebas: [T.estado(200), T.prueba('inactiva', (j, v) => { pm.expect(j.id).to.equal(v('d_empresa_id')); pm.expect(j.activa).to.equal(false); })],
    }),
    peticion({
      nombre: 'D4 Comprobar que la empresa está inactiva (consulta por id y catálogo de activas)', ruta: '/empresas/{{d_empresa_id}}', token: 'd_token_super',
      pruebas: [T.estado(200), T.prueba('activa=false', (j) => { pm.expect(j.activa).to.equal(false); })],
    }),
    peticion({
      nombre: 'D5 La empresa inactiva no figura en el catálogo de activas', ruta: '/empresas', consulta: { solo_activas: 'true' }, token: 'd_token_super',
      pruebas: [T.estado(200), T.prueba('ausente', (j, v) => { pm.expect(j.map((e) => e.id)).to.not.include(v('d_empresa_id')); })],
    }),
    peticion({
      nombre: 'D6 Crear la operación de ingesta', metodo: 'POST', ruta: '/documentos/operaciones', token: 'd_token_super',
      pruebas: [T.estado(201), T.guardar((j, set) => set('d_operacion_id', j.operacion_id))],
    }),
    peticion({
      nombre: 'D7 Intentar ingerir un Markdown válido para la empresa inactiva → 400 COMPANY_INACTIVE', metodo: 'POST', ruta: '/documentos/operaciones/{{d_operacion_id}}/ingesta', token: 'd_token_super',
      descripcion: 'Contenido válido y exclusivo de este flujo (distinto de B/C): el rechazo no puede deberse a formato ni a duplicados.',
      formulario: [
        { key: 'archivo', src: 'muestras/d-reporte.md' }, { key: 'empresa_id', value: '{{d_empresa_id}}' },
        { key: 'anio', value: '{{anio_b}}' }, { key: 'tipo_documento', value: 'REPORTE_SOSTENIBILIDAD_GRI' },
      ],
      pruebas: [
        T.error(400, 'COMPANY_INACTIVE'),
        T.prueba('el rechazo no es una ingesta exitosa', (j) => { pm.expect(j).to.not.have.property('exitosa'); pm.expect(j).to.not.have.property('documento_id'); }),
      ],
    }),
    peticion({
      nombre: 'D8 La operación informa el rechazo esperado', ruta: '/documentos/operaciones/{{d_operacion_id}}', token: 'd_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('RECHAZADA con COMPANY_INACTIVE, sin documento, sin análisis, no exitosa', (j) => {
          pm.expect(j.estado).to.equal('RECHAZADA');
          pm.expect(j.terminal).to.equal(true);
          pm.expect(j.exitosa).to.equal(false);
          pm.expect(j.documento_id).to.equal(null);
          pm.expect(j.resultado_analisis).to.equal(null);
          pm.expect(j.fragmentos_procesados).to.equal(0);
          pm.expect(j.error.code).to.equal('COMPANY_INACTIVE');
        }),
      ],
    }),
    peticion({
      nombre: 'D9 La empresa continúa inactiva', ruta: '/empresas/{{d_empresa_id}}', token: 'd_token_super',
      pruebas: [T.estado(200), T.prueba('activa=false', (j) => { pm.expect(j.activa).to.equal(false); })],
    }),
    peticion({
      nombre: 'D10 No existe ningún documento (ni disponible para RAG) de esa empresa', ruta: '/documentos', consulta: { empresa_id: '{{d_empresa_id}}' }, token: 'd_token_super',
      descripcion: SOLO_HTTP,
      pruebas: [T.estado(200), T.prueba('cero documentos', (j) => { pm.expect(j.total).to.equal(0); pm.expect(j.items).to.deep.equal([]); })],
    }),
    crearAdministrador('D11 Crear un Administrador de prueba para consultar el análisis', 'd', 'D'),
    loginAdministrador('D12 Autenticar al Administrador de prueba', 'd'),
    peticion({
      nombre: 'D13 No hay análisis utilizable del documento rechazado', ruta: '/analisis-gri/empresas/{{d_empresa_id}}/anios/{{anio_b}}', token: 'd_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('DOCUMENTACION_INSUFICIENTE: sin documentos, filas, sanciones ni puntaje', (j) => {
          pm.expect(j.estado_flujo).to.equal('DOCUMENTACION_INSUFICIENTE');
          pm.expect(j.habilita_reporte).to.equal(false);
          pm.expect([j.documentos, j.filas, j.sanciones]).to.deep.equal([[], [], []]);
          pm.expect(j.puntaje.estado).to.equal('NO_DISPONIBLE');
          pm.expect(j.puntaje.valor).to.equal(null);
          pm.expect(j.validacion).to.equal(null);
        }),
      ],
    }),
    peticion({
      nombre: 'D14 Cierre: deshabilitar al Administrador de D', metodo: 'PATCH', ruta: '/usuarios/{{d_admin_id}}/deshabilitar', token: 'd_token_super',
      pruebas: [T.estado(200), T.prueba('cuenta deshabilitada', (j) => { pm.expect(j.habilitado).to.equal(false); })],
    }),
  ]);

// ---- E. Reportes de prospección (PDF) ------------------------------------------------------------------------------------
// Requiere a B (su análisis queda VALIDADO en B15) con la empresa activa y el Administrador habilitado: Z los cierra después. Ejecutar `--carpeta B --carpeta E`.

const flujoE = carpeta('E. Reportes de prospección (PDF)',
  'Sobre el análisis VALIDADO de B: un Administrador genera el reporte, lo lista, descarga el PDF y pide la vista previa; un SuperAdmin no puede (403); sin sesión 401; y una empresa sin análisis validado se rechaza (409). Requiere a B.',
  [
    peticion({
      nombre: 'E1 Generar el reporte del análisis validado → 201', metodo: 'POST', ruta: '/reportes', token: 'b_token_admin',
      json: { empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' },
      pruebas: [
        T.estado(201),
        T.prueba('ReporteResumen: mismo empresa/año, autor = Administrador de la sesión, sha256 del PDF', (j, v) => {
          pm.expect(esUuid(j.id)).to.equal(true);
          pm.expect(j.empresa_id).to.equal(v('b_empresa_id'));
          pm.expect(j.anio).to.equal(Number(v('anio_b')));
          pm.expect(j.generado_por).to.equal(v('b_admin_id'));
          pm.expect(esHex64(j.sha256), 'sha256 del PDF').to.equal(true);
          pm.expect(esFecha(j.creado_en)).to.equal(true);
        }),
        T.guardarCampos({ e_reporte_id: 'j.id', e_sha256: 'j.sha256' }),
      ],
    }),
    peticion({
      nombre: 'E2 El historial contiene el reporte recién generado', ruta: '/reportes', token: 'b_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('el reporte de la empresa/año está en el historial, con su sha256', (j, v) => {
          pm.expect(j).to.be.an('array');
          const m = j.find((r) => r.id === v('e_reporte_id'));
          pm.expect(m, 'el reporte generado figura en el historial').to.not.equal(undefined);
          pm.expect(m.sha256).to.equal(v('e_sha256'));
        }),
      ],
    }),
    peticion({
      nombre: 'E3 Descargar el PDF del reporte → application/pdf', ruta: '/reportes/{{e_reporte_id}}/archivo', token: 'b_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('respuesta PDF descargable (Content-Type + Content-Disposition + cabecera %PDF)', () => {
          pm.expect(pm.response.headers.get('Content-Type') || '').to.include('application/pdf');
          pm.expect(pm.response.headers.get('Content-Disposition') || '').to.include('attachment');
          pm.expect(pm.response.text().slice(0, 5)).to.equal('%PDF-');
        }),
      ],
    }),
    peticion({
      nombre: 'E3b Una segunda generación para la misma empresa/año se rechaza → 409 REPORT_ALREADY_EXISTS', metodo: 'POST', ruta: '/reportes', token: 'b_token_admin',
      json: { empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' },
      descripcion: 'El reporte es inmutable (RN-041): no hay versiones, sustitución ni regeneración. El conflicto identifica el reporte existente.',
      pruebas: [
        T.error(409, 'REPORT_ALREADY_EXISTS', (d, v) => {
          pm.expect(d.reporte_id).to.equal(v('e_reporte_id'));
          pm.expect(d.empresa_id).to.equal(v('b_empresa_id'));
          pm.expect(d.anio).to.equal(Number(v('anio_b')));
        }),
      ],
    }),
    peticion({
      nombre: 'E3c El reporte original sigue intacto en el historial tras el rechazo (mismo id y sha256)', ruta: '/reportes', token: 'b_token_admin',
      pruebas: [
        T.estado(200),
        T.prueba('un único reporte de la empresa/año, el original, con su sha256', (j, v) => {
          const delaEmpresa = j.filter((r) => r.empresa_id === v('b_empresa_id') && r.anio === Number(v('anio_b')));
          pm.expect(delaEmpresa).to.have.length(1);
          pm.expect(delaEmpresa[0].id).to.equal(v('e_reporte_id'));
          pm.expect(delaEmpresa[0].sha256).to.equal(v('e_sha256'));
        }),
      ],
    }),
    peticion({
      nombre: 'E4 Vista previa del reporte (misma plantilla, sin guardar) → application/pdf', metodo: 'POST', ruta: '/reportes/vista-previa', token: 'b_token_admin',
      json: { empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' },
      pruebas: [
        T.estado(200),
        T.prueba('la vista previa es un PDF (no persiste ni sustituye el reporte existente: E3c ya lo comprobó)', () => {
          pm.expect(pm.response.headers.get('Content-Type') || '').to.include('application/pdf');
          pm.expect(pm.response.text().slice(0, 5)).to.equal('%PDF-');
        }),
      ],
    }),
    peticion({
      nombre: 'E5 Un SuperAdmin no genera reportes → 403', metodo: 'POST', ruta: '/reportes', token: 'b_token_super',
      json: { empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' }, pruebas: [T.prohibido()],
    }),
    peticion({
      nombre: 'E6 Sin sesión no hay reportes → 401', ruta: '/reportes', pruebas: [T.sinSesion()],
    }),
    peticion({
      nombre: 'E7 (prep) Crear una empresa sin documentos (sin análisis validable)', metodo: 'POST', ruta: '/empresas', token: 'b_token_super',
      json: { nombre: 'E2E {{run_id}} Empresa E', sector: 'MINERIA' },
      pruebas: [T.estado(201), T.guardarCampos({ e_empresa_sin_id: 'j.id' })],
    }),
    peticion({
      nombre: 'E8 Reporte de una empresa/año SIN análisis validado → 409 ANALYSIS_NOT_VALIDATED', metodo: 'POST', ruta: '/reportes', token: 'b_token_admin',
      json: { empresa_id: '{{e_empresa_sin_id}}', anio: '{{anio_b}}' },
      descripcion: 'La empresa de E7 no tiene documentos: no hay validación vigente y el reporte se bloquea.',
      pruebas: [T.error(409, 'ANALYSIS_NOT_VALIDATED')],
    }),
    peticion({
      nombre: 'E9 (cierre) Desactivar la empresa de E7', metodo: 'PATCH', ruta: '/empresas/{{e_empresa_sin_id}}/desactivar', token: 'b_token_super',
      pruebas: [T.estado(200), T.prueba('inactiva', (j) => { pm.expect(j.activa).to.equal(false); })],
    }),
  ]);

// ---- F. Asistente de IA (RAG) --------------------------------------------------------------------------------------------
// Requiere a B (su documento queda COMPLETADO y publicado, con empresa activa y Administrador habilitado). Ejecutar `--carpeta B --carpeta F`.
// Necesita embeddings (OCI) y LLM configurados en el entorno; si faltan, el endpoint responde 503 y la corrida lo refleja.

const flujoF = carpeta('F. Asistente de IA (RAG)',
  'Sobre el documento publicado de B: un Administrador consulta al asistente y recibe respuesta con fuentes verificables (el caso positivo exige evidencia); un año sin documentos declara la ausencia de evidencia; sin sector 422; un SuperAdmin no puede (403); sin sesión 401. Requiere a B y OCI+LLM configurados.',
  [
    peticion({
      nombre: 'F1 Consulta positiva con sector, empresa y año de B → respuesta CON evidencia verificable', metodo: 'POST', ruta: '/asistente/consultas', token: 'b_token_admin',
      descripcion: 'Caso positivo: la muestra de B dice que el consumo energético se reporta según GRI 302-1, así que el asistente DEBE responder con fuentes. Una respuesta «sin evidencia» NO es éxito aquí (la ausencia de evidencia se prueba en F1d). Depende de que OCI y el proveedor de chat respondan; si el modelo no respalda su respuesta, el caso falla a propósito.',
      json: { pregunta: '¿Según qué estándar GRI se reporta el consumo energético de la organización?', sector: 'ENERGIA', empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' },
      pruebas: [
        T.estado(200),
        T.prueba('ConsultaResponse: respuesta, afirmaciones, citas, bandera de evidencia, motivo, mensaje y descartadas', (j) => {
          pm.expect(Object.keys(j)).to.have.members(['respuesta', 'afirmaciones', 'citas', 'sin_evidencia', 'motivo', 'mensaje', 'afirmaciones_descartadas']);
          pm.expect(j.respuesta).to.be.a('string');
          pm.expect(j.afirmaciones).to.be.an('array');
          pm.expect(j.citas).to.be.an('array');
          pm.expect(j.sin_evidencia).to.be.a('boolean');
        }),
        T.prueba('hay evidencia: «sin evidencia» no cuenta como éxito del caso positivo', (j) => {
          pm.expect(j.sin_evidencia, 'se esperaba una respuesta con evidencia; motivo informado: ' + j.motivo).to.equal(false);
          pm.expect(j.motivo).to.equal(null);
          pm.expect(j.respuesta).to.be.a('string').and.not.empty;
          pm.expect(j.afirmaciones.length).to.be.above(0);
          pm.expect(j.citas.length).to.be.above(0);
        }),
        T.prueba('cada afirmación enlaza con sus fuentes y su cita textual consta literalmente en el documento enviado', (j, v) => {
          const normalizar = (t) => String(t).normalize('NFKC').toLowerCase().replace(/\|/g, ' ').replace(/[*`]/g, '').split(/\s+/).filter(Boolean).join(' ');
          const documento = normalizar(v('texto_b'));
          const referencias = j.citas.map((c) => c.referencia);
          j.afirmaciones.forEach((a) => {
            pm.expect(a.texto).to.be.a('string').and.not.empty;
            pm.expect(a.referencias.length).to.be.above(0);
            a.referencias.forEach((n) => {
              pm.expect(referencias, 'la referencia ' + n + ' figura en citas').to.include(n);
              pm.expect(j.respuesta, 'el texto de la respuesta lleva el marcador [' + n + ']').to.include('[' + n + ']');
            });
            pm.expect(documento.includes(normalizar(a.cita_textual)), 'la cita textual figura en el documento de B').to.equal(true);
          });
          const usadas = Array.from(new Set([].concat(...j.afirmaciones.map((a) => a.referencias)))).sort();
          pm.expect(referencias.slice().sort(), 'solo se muestran las fuentes que respaldan alguna afirmación').to.deep.equal(usadas);
        }),
        T.prueba('cada fuente identifica documento, empresa y año de B', (j, v) => {
          j.citas.forEach((c) => {
            pm.expect(c.documento_id).to.equal(v('b_documento_id'));
            pm.expect(c.documento_nombre).to.equal(v('nombre_b'));
            pm.expect(c.empresa_id).to.equal(v('b_empresa_id'));
            pm.expect(c.empresa_nombre).to.equal('E2E ' + v('run_id') + ' Empresa B');
            pm.expect(c.anio).to.equal(Number(v('anio_b')));
            pm.expect(c.seccion).to.be.a('string');
            pm.expect(c.extracto).to.be.a('string').and.not.empty;
          });
        }),
      ],
    }),
    peticion({
      nombre: 'F1d Consulta de un año sin documentos → ausencia de evidencia explícita (SIN_FRAGMENTOS)', metodo: 'POST', ruta: '/asistente/consultas', token: 'b_token_admin',
      descripcion: 'Caso de ausencia de evidencia determinista: B no tiene documentos del año del intento duplicado de C (C2 fue rechazado), así que no hay fragmentos y el servicio no consulta al modelo.',
      json: { pregunta: '¿Según qué estándar GRI se reporta el consumo energético de la organización?', sector: 'ENERGIA', empresa_id: '{{b_empresa_id}}', anio: '{{anio_c}}' },
      pruebas: [
        T.estado(200),
        T.prueba('sin_evidencia con su motivo y mensaje; sin respuesta, afirmaciones ni fuentes', (j) => {
          pm.expect(j.sin_evidencia).to.equal(true);
          pm.expect(j.motivo).to.equal('SIN_FRAGMENTOS');
          pm.expect(j.mensaje).to.be.a('string').and.not.empty;
          pm.expect(j.respuesta).to.equal('');
          pm.expect(j.afirmaciones).to.have.length(0);
          pm.expect(j.citas).to.have.length(0);
        }),
      ],
    }),
    peticion({
      nombre: 'F1b Sin sector no hay consulta → 422 (la selección es obligatoria)', metodo: 'POST', ruta: '/asistente/consultas', token: 'b_token_admin',
      json: { pregunta: '¿Qué indicadores GRI reporta la empresa?', empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' },
      pruebas: [T.validacion('sector')],
    }),
    peticion({
      nombre: 'F1c Sector que no corresponde a la empresa → 400 COMPANY_SECTOR_MISMATCH', metodo: 'POST', ruta: '/asistente/consultas', token: 'b_token_admin',
      json: { pregunta: '¿Qué indicadores GRI reporta la empresa?', sector: 'PETROLEO', empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' },
      pruebas: [T.error(400, 'COMPANY_SECTOR_MISMATCH')],
    }),
    peticion({
      nombre: 'F2 Un SuperAdmin no usa el asistente → 403', metodo: 'POST', ruta: '/asistente/consultas', token: 'b_token_super',
      json: { pregunta: '¿Qué GRI reporta?', sector: 'ENERGIA', empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' }, pruebas: [T.prohibido()],
    }),
    peticion({
      nombre: 'F3 Sin sesión no hay asistente → 401', metodo: 'POST', ruta: '/asistente/consultas',
      json: { pregunta: 'x', sector: 'ENERGIA', empresa_id: '{{b_empresa_id}}', anio: '{{anio_b}}' }, pruebas: [T.sinSesion()],
    }),
  ]);

// ---- G. Auditoría funcional ----------------------------------------------------------------------------------------------
// El SuperAdmin consulta los eventos sensibles generados por los flujos anteriores. Requiere a B (sesión vigente del Administrador para G3/G3b).

const flujoG = carpeta('G. Auditoría funcional',
  'Un SuperAdmin consulta el panel de auditoría con filtros y paginación; un Administrador no puede (403); sin sesión 401.',
  [
    peticion({
      nombre: 'G1 Listar eventos de auditoría (paginado) → 200', ruta: '/auditoria', consulta: { pagina: '1', tamano: '5' }, token: 'b_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('PaginaEventos: items, total, pagina y tamano', (j) => {
          pm.expect(Object.keys(j)).to.have.members(['items', 'pagina', 'tamano', 'total']);
          pm.expect(j.items).to.be.an('array');
          pm.expect(j.pagina).to.equal(1);
          pm.expect(j.tamano).to.equal(5);
          pm.expect(j.total).to.be.at.least(j.items.length);
          j.items.forEach((e) => {
            pm.expect(esUuid(e.id)).to.equal(true);
            pm.expect(e.tipo_evento).to.be.a('string').and.not.empty;
            pm.expect(e.detalle).to.be.a('string');
            pm.expect(esFecha(e.fecha_hora_utc)).to.equal(true);
            pm.expect([null, 'EXITOSO', 'FALLIDO', 'DENEGADO'], 'resultado (nulo solo en eventos anteriores a la migración 0005)').to.include(e.resultado);
          });
        }),
      ],
    }),
    peticion({
      nombre: 'G2 Filtrar por tipo de evento (Inicio de sesión) → 200', ruta: '/auditoria', consulta: { tipo_evento: 'Inicio de sesión' }, token: 'b_token_super',
      pruebas: [
        T.estado(200),
        T.prueba('todos los eventos devueltos son del tipo filtrado', (j) => {
          pm.expect(j.items).to.be.an('array');
          j.items.forEach((e) => pm.expect(e.tipo_evento).to.equal('Inicio de sesión'));
        }),
      ],
    }),
    peticion({
      nombre: 'G3 Un Administrador no accede a la auditoría → 403', ruta: '/auditoria', token: 'b_token_admin', pruebas: [T.prohibido()],
    }),
    peticion({
      nombre: 'G3b El intento denegado del Administrador quedó registrado (ACCESO_DENEGADO, DENEGADO) a nombre de su cuenta', ruta: '/auditoria', token: 'b_token_super',
      consulta: { tipo_evento: 'Acceso denegado', usuario_id: '{{b_admin_id}}' },
      descripcion: 'Requiere que G3 se haya ejecutado con una sesión VIGENTE del Administrador de B: su cierre ocurre en Z, después de G.',
      pruebas: [
        T.estado(200),
        T.prueba('al menos un evento de acceso denegado del Administrador, con resultado DENEGADO', (j, v) => {
          pm.expect(j.items.length).to.be.above(0);
          j.items.forEach((e) => { pm.expect(e.usuario_id).to.equal(v('b_admin_id')); pm.expect(e.resultado).to.equal('DENEGADO'); });
        }),
      ],
    }),
    peticion({
      nombre: 'G4 Sin sesión no hay auditoría → 401', ruta: '/auditoria', pruebas: [T.sinSesion()],
    }),
  ]);

// ---- Z. Cierre de los datos sintéticos de B ------------------------------------------------------------------------------
// No es un flujo funcional: deja inertes la empresa y el Administrador de B DESPUÉS de todos sus consumidores (C, E, F, G).
// No hay endpoints de borrado; solo se usan los de desactivar/deshabilitar y solo sobre los datos creados por esta ejecución.

const cierreB = carpeta('Z. Cierre de los datos sintéticos de B',
  'Desactiva la empresa de B y deshabilita a su Administrador cuando ya terminaron todos los flujos que los usan (C, E, F, G). Va siempre al final y el ejecutor la añade cuando B forma parte de la selección. Solo toca datos creados por esta ejecución.',
  [
    peticion({
      nombre: 'Z1 Cierre: desactivar la empresa de B', metodo: 'PATCH', ruta: '/empresas/{{b_empresa_id}}/desactivar', token: 'b_token_super',
      pruebas: [T.estado(200), T.prueba('empresa de esta ejecución, inactiva', (j, v) => { pm.expect(j.id).to.equal(v('b_empresa_id')); pm.expect(j.activa).to.equal(false); })],
    }),
    peticion({
      nombre: 'Z2 Cierre: deshabilitar al Administrador de B', metodo: 'PATCH', ruta: '/usuarios/{{b_admin_id}}/deshabilitar', token: 'b_token_super',
      pruebas: [T.estado(200), T.prueba('cuenta de esta ejecución, deshabilitada', (j, v) => { pm.expect(j.id).to.equal(v('b_admin_id')); pm.expect(j.habilitado).to.equal(false); })],
    }),
    peticion({
      nombre: 'Z3 La sesión del Administrador de B quedó revocada → 401', ruta: '/empresas', consulta: { solo_activas: 'true' }, token: 'b_token_admin',
      descripcion: 'Comprueba que el cierre tuvo efecto (misma regla que A7). Si esta petición se ejecutara antes que E, F o G, esos flujos recibirían este 401.',
      pruebas: [T.sinSesion()],
    }),
  ]);

export const coleccionE2E = coleccion({
  nombre: 'Igualab · E2E HTTP del backend (flujos A–G)',
  descripcion:
    'Flujos A (administración y revocación de acceso), B (ingesta y validación GRI), C (duplicado por contenido), D (empresa inactiva), ' +
    'E (reportes de prospección en PDF), F (asistente de IA / RAG) y G (auditoría funcional) sobre el backend desplegado. ' +
    'Sin mocks. B usa OCI real con una muestra mínima. C, E, F y G requieren a B; la carpeta Z cierra los datos de B al final. Ver pruebas-http/README.md.',
  variables: {
    base_url: 'http://localhost:8000',
    requisitos_carpetas: JSON.stringify(REQUISITOS_E2E),
    cierres_carpetas: JSON.stringify(CIERRES_E2E),
  },
  items: [flujoA, flujoB, flujoC, flujoD, flujoE, flujoF, flujoG, cierreB],
});
