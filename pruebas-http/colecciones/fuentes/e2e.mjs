/**
 * Colección E2E por HTTP: los CUATRO flujos confirmados del backend sobre el sistema DESPLEGADO (sin dobles ni mocks).
 *
 *  A. Administración y revocación de acceso.
 *  B. Ingesta y validación GRI (ingiere un Markdown sintético: usa OCI real con una muestra mínima).
 *  C. Rechazo de duplicado por contenido (usa el documento completado de B: ejecutar B→C en la misma corrida).
 *  D. Rechazo de ingesta para una empresa inactiva.
 *
 * Cada flujo prepara sus propios datos con el identificador de ejecución `run_id` (A y D son independientes; C necesita a B
 * y por eso se ejecuta `--carpeta B --carpeta C`). Todo ID, token, versión y huella sale de la respuesta anterior.
 * No hay endpoints de borrado: ver README «Datos que quedan». Nunca se transfiere el rol SuperAdmin.
 */
import { T, carpeta, coleccion, peticion } from './dsl.mjs';

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
  'Reenvía EXACTAMENTE los mismos bytes del documento de B con otro nombre de archivo y otro año (así solo coincide el SHA-256, no empresa/año/tipo). Requiere a B: ejecutar `--carpeta B --carpeta C`.',
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
    peticion({
      nombre: 'C7 Cierre: desactivar la empresa de B', metodo: 'PATCH', ruta: '/empresas/{{b_empresa_id}}/desactivar', token: 'b_token_super',
      pruebas: [T.estado(200), T.prueba('empresa inactiva', (j) => { pm.expect(j.activa).to.equal(false); })],
    }),
    peticion({
      nombre: 'C8 Cierre: deshabilitar al Administrador de B', metodo: 'PATCH', ruta: '/usuarios/{{b_admin_id}}/deshabilitar', token: 'b_token_super',
      pruebas: [T.estado(200), T.prueba('cuenta deshabilitada', (j) => { pm.expect(j.habilitado).to.equal(false); })],
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

export const coleccionE2E = coleccion({
  nombre: 'Igualab · E2E HTTP del backend (cuatro flujos)',
  descripcion:
    'Flujos A (administración y revocación de acceso), B (ingesta y validación GRI), C (duplicado por contenido) y D (empresa inactiva) sobre el backend desplegado. ' +
    'Sin mocks. B usa OCI real con una muestra mínima. Ver pruebas-http/README.md.',
  variables: { base_url: 'http://localhost:8000' },
  items: [flujoA, flujoB, flujoC, flujoD],
});
