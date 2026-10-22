"""Pruebas de pruebas-zap/hooks/zap_hooks.py con un cliente de ZAP FALSO (sin ZAP, sin red).

Verifican la lógica del hook: inyección de credenciales por rol, exclusiones, comprobación de alcance antes del ataque, cobertura
autenticada, estado real del análisis activo y que ningún mensaje de error pueda contener un token.

    python -I pruebas-zap/pruebas/test_zap_hooks.py
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = 'http://igualab-backend-qa-backend-1:8000'
TOKEN_SUPER = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzdXBlciJ9.firmaSuperSecreta123'
TOKEN_ADMIN = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.firmaAdminSecreta456'


def cargar_hook(directorio_trabajo, directorio_privado):
    os.environ['ZAP_HOOK_DIR_TRABAJO'] = directorio_trabajo
    os.environ['ZAP_HOOK_DIR_PRIVADO'] = directorio_privado
    especificacion = importlib.util.spec_from_file_location('zap_hooks_bajo_prueba', RAIZ / 'hooks' / 'zap_hooks.py')
    modulo = importlib.util.module_from_spec(especificacion)
    especificacion.loader.exec_module(modulo)
    return modulo


def mensaje(metodo, ruta, estado, autorizacion=True, origen=DESTINO, token=TOKEN_SUPER):
    cabecera = f'{metodo} {origen}{ruta} HTTP/1.1\r\nHost: x\r\n'
    if autorizacion:
        cabecera += 'Authorization: Bearer ' + token + '\r\n'
    return {'requestHeader': cabecera, 'responseHeader': f'HTTP/1.1 {estado} X\r\n', 'requestBody': '', 'responseBody': ''}


CONFIG = {
    'destino': DESTINO,
    'roles': {
        'superadmin': {'url_regex': '^' + DESTINO + '/(usuarios|empresas)([/?].*)?$'},
        'administrador': {'url_regex': '^' + DESTINO + '/(reportes)([/?].*)?$'},
    },
    'minimos_autenticados_por_rol': {'superadmin': 1, 'administrador': 1},
    'incluidas': [
        {'metodo': 'GET', 'ruta': '/usuarios', 'rol': 'superadmin', 'regex': '^/usuarios/?$'},
        {'metodo': 'GET', 'ruta': '/empresas/{empresa_id}', 'rol': 'superadmin', 'regex': '^/empresas/[^/]+/?$'},
        {'metodo': 'GET', 'ruta': '/reportes', 'rol': 'administrador', 'regex': '^/reportes/?$'},
        {'metodo': 'POST', 'ruta': '/auth/login', 'rol': 'publico', 'regex': '^/auth/login/?$'},
    ],
    'excluidas': [
        {'metodo': 'POST', 'ruta': '/auth/logout', 'categoria': 'sesion', 'regex': '^/auth/logout/?$'},
        {'metodo': 'POST', 'ruta': '/usuarios', 'categoria': 'cuentas', 'regex': '^/usuarios/?$'},
    ],
    'excluir_del_ataque': ['^' + DESTINO + '/auth/logout/?(\\?.*)?$'],
    'neutralizar_metodos': [
        {'ruta': '/usuarios', 'metodos': ['POST'], 'metodo_regex': '^(POST) ', 'url_regex': '^' + DESTINO + '/usuarios/?(\\?.*)?$'},
        {'ruta': '/auth/logout', 'metodos': ['*'], 'metodo_regex': '^[A-Z]+ ', 'url_regex': '^' + DESTINO + '/auth/logout/?(\\?.*)?$'},
    ],
    'url_sin_credencial_regex': '^(?!' + DESTINO + '/(usuarios|empresas|reportes)([/?].*)?$).*',
}


class ZapFalso:
    """Imita la parte del cliente de ZAP que usa el hook."""

    class _Replacer:
        pass

    def __init__(self, mensajes=(), urls=None, escaneos=None, progreso=None, fallar_en=None):
        self.base = 'http://zap/JSON/'
        self.solicitudes = []
        self.exclusiones = []
        self._mensajes = list(mensajes)
        self._urls = urls if urls is not None else [DESTINO + '/usuarios']
        self._escaneos = escaneos if escaneos is not None else []
        self._progreso = progreso
        self._fallar_en = fallar_en
        zap = self

        class Core:
            def urls(self_inner):
                return zap._urls

            def messages(self_inner, start='0', count='500'):
                return zap._mensajes[int(start):int(start) + int(count)]

        class Ascan:
            scans = property(lambda self_inner: zap._escaneos)

            def exclude_from_scan(self_inner, patron):
                zap.exclusiones.append(('ascan', patron))

            def scan_progress(self_inner, id_):
                return zap._progreso

        class Spider:
            def exclude_from_scan(self_inner, patron):
                zap.exclusiones.append(('spider', patron))

        self.core, self.ascan, self.spider = Core(), Ascan(), Spider()

    def _request(self, url, parametros=None):
        self.solicitudes.append((url, dict(parametros or {})))
        if self._fallar_en == 'replacer':
            # El cliente real mete los parámetros en la URL: una excepción podría contenerlos.
            raise RuntimeError(f'fallo al llamar {url}?replacement=Bearer {TOKEN_SUPER}')
        return {'Result': 'OK'}


class PruebasDelHook(unittest.TestCase):
    def setUp(self):
        self.trabajo = tempfile.TemporaryDirectory()
        self.privado = tempfile.TemporaryDirectory()
        Path(self.trabajo.name, 'hook-config.json').write_text(json.dumps(CONFIG), encoding='utf-8')
        Path(self.privado.name, 'credenciales.json').write_text(json.dumps({'superadmin': TOKEN_SUPER, 'administrador': TOKEN_ADMIN}), encoding='utf-8')
        self.hook = cargar_hook(self.trabajo.name, self.privado.name)

    def tearDown(self):
        self.trabajo.cleanup()
        self.privado.cleanup()

    # ------------------------------------------------------------------------------------------------------- inyección
    def test_zap_started_agrega_una_regla_por_rol_con_filtro_de_url_y_excluye_del_ataque(self):
        zap = ZapFalso()
        self.hook.zap_started(zap, DESTINO)
        reglas = {p['description']: p for u, p in zap.solicitudes if u.endswith('replacer/action/addRule/') and p['description'].startswith('igualab-autenticacion-')}
        self.assertEqual(set(reglas), {'igualab-autenticacion-superadmin', 'igualab-autenticacion-administrador'})
        self.assertEqual(reglas['igualab-autenticacion-superadmin']['replacement'], 'Bearer ' + TOKEN_SUPER)
        self.assertEqual(reglas['igualab-autenticacion-administrador']['replacement'], 'Bearer ' + TOKEN_ADMIN)
        for regla in reglas.values():
            self.assertEqual(regla['matchType'], 'REQ_HEADER')
            self.assertEqual(regla['matchString'], 'Authorization')
            self.assertEqual(regla['matchRegex'], 'false')
            self.assertTrue(regla['url'].startswith('^' + DESTINO))
        self.assertIn(('ascan', '^' + DESTINO + '/auth/logout/?(\\?.*)?$'), zap.exclusiones)
        self.assertIn(('spider', '^' + DESTINO + '/auth/logout/?(\\?.*)?$'), zap.exclusiones)

    def test_un_error_del_cliente_nunca_expone_el_token(self):
        zap = ZapFalso(fallar_en='replacer')
        with self.assertRaises(RuntimeError) as contexto:
            self.hook.zap_started(zap, DESTINO)
        self.assertNotIn(TOKEN_SUPER, str(contexto.exception))
        self.assertNotIn('Bearer', str(contexto.exception))
        self.assertIsNone(contexto.exception.__cause__, 'la causa original (con la URL y el token) no debe encadenarse')
        self.assertTrue(contexto.exception.__suppress_context__)

    def test_sin_credencial_para_un_rol_aborta(self):
        Path(self.privado.name, 'credenciales.json').write_text(json.dumps({'superadmin': TOKEN_SUPER}), encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.hook.zap_started(ZapFalso(), DESTINO)

    # -------------------------------------------------------------------------------------------------------- análisis
    def test_cobertura_distingue_401_403_de_acceso_real(self):
        mensajes = [
            mensaje('GET', '/usuarios', 200), mensaje('GET', '/usuarios', 401, autorizacion=False),
            mensaje('GET', '/empresas/abc', 404), mensaje('GET', '/reportes', 403),
        ]
        analisis = self.hook.analizar_mensajes(mensajes, CONFIG)
        self.assertEqual(analisis['por_rol']['superadmin'], {'autenticadas': 2, 'alcanzaron_la_logica': 2, 'exitosas_2xx': 1})
        self.assertEqual(analisis['por_rol']['administrador'], {'autenticadas': 1, 'alcanzaron_la_logica': 0, 'exitosas_2xx': 0})
        cobertura = self.hook.cobertura_autenticada(analisis, CONFIG)
        self.assertFalse(cobertura['demostrada'], 'el Administrador solo obtuvo 403: no hay cobertura demostrada')
        self.assertIn('GET /reportes', cobertura['operaciones_sin_cobertura_autenticada'])

    def test_cobertura_demostrada_con_2xx_en_todos_los_roles(self):
        analisis = self.hook.analizar_mensajes([mensaje('GET', '/usuarios', 200), mensaje('GET', '/reportes', 200)], CONFIG)
        self.assertTrue(self.hook.cobertura_autenticada(analisis, CONFIG)['demostrada'])

    def test_una_respuesta_sin_la_cabecera_authorization_no_cuenta_como_autenticada(self):
        analisis = self.hook.analizar_mensajes([mensaje('GET', '/usuarios', 200, autorizacion=False)], CONFIG)
        self.assertEqual(analisis['por_rol']['superadmin']['autenticadas'], 0)

    def test_se_detecta_una_peticion_a_una_operacion_excluida_y_no_a_un_metodo_incluido_de_la_misma_ruta(self):
        mensajes = [mensaje('POST', '/auth/logout', 204), mensaje('POST', '/usuarios', 201), mensaje('GET', '/usuarios', 200)]
        analisis = self.hook.analizar_mensajes(mensajes, CONFIG)
        self.assertEqual(analisis['violaciones_total'], 2)
        self.assertEqual({(v['metodo'], v['ruta']) for v in analisis['violaciones']}, {('POST', '/auth/logout'), ('POST', '/usuarios')})

    def test_origenes_ajenos_se_registran_y_no_cuentan_para_la_cobertura(self):
        analisis = self.hook.analizar_mensajes([mensaje('GET', '/usuarios', 200, origen='http://otro-host:8000')], CONFIG)
        self.assertEqual(analisis['origenes_ajenos'], ['http://otro-host:8000'])
        self.assertEqual(analisis['por_rol']['superadmin']['autenticadas'], 0)

    def test_el_analisis_no_conserva_cabeceras_ni_cuerpos(self):
        analisis = self.hook.analizar_mensajes([mensaje('GET', '/usuarios', 200)], CONFIG)
        self.assertNotIn(TOKEN_SUPER, json.dumps(analisis))

    # ----------------------------------------------------------------------------------------- credencial correcta por rol
    TOKENS = {'superadmin': TOKEN_SUPER, 'administrador': TOKEN_ADMIN}

    def test_cada_url_lleva_el_token_de_su_rol_y_no_se_detecta_mezcla(self):
        mensajes = [
            mensaje('GET', '/usuarios', 200, token=TOKEN_SUPER), mensaje('GET', '/empresas/abc', 404, token=TOKEN_SUPER),
            mensaje('GET', '/reportes', 200, token=TOKEN_ADMIN), mensaje('POST', '/auth/login', 401, autorizacion=False),
        ]
        self.assertEqual(self.hook.analizar_mensajes(mensajes, CONFIG, self.TOKENS)['autorizacion_incorrecta_total'], 0)

    def test_se_detecta_el_token_de_otro_rol_en_una_url(self):
        analisis = self.hook.analizar_mensajes([mensaje('GET', '/reportes', 403, token=TOKEN_SUPER)], CONFIG, self.TOKENS)
        self.assertEqual(analisis['autorizacion_incorrecta_total'], 1)
        self.assertEqual(analisis['autorizacion_incorrecta'][0], {'metodo': 'GET', 'ruta': '/reportes', 'rol_esperado': 'administrador', 'rol_del_token': 'superadmin'})

    def test_se_detecta_un_token_real_hacia_un_host_ajeno_y_hacia_una_ruta_sin_regla_de_rol(self):
        mensajes = [
            mensaje('GET', '/usuarios', 200, origen='http://otro-host:8000'),
            mensaje('POST', '/auth/login', 401, token=TOKEN_ADMIN),
            mensaje('GET', '/latest/meta-data', 404, token=TOKEN_ADMIN),
        ]
        analisis = self.hook.analizar_mensajes(mensajes, CONFIG, self.TOKENS)
        self.assertEqual(analisis['autorizacion_incorrecta_total'], 3)
        self.assertEqual(analisis['autorizacion_incorrecta'][0]['ruta'], '(host ajeno)')
        self.assertEqual(analisis['autorizacion_incorrecta'][1]['rol_esperado'], 'ninguno')
        self.assertEqual(analisis['autorizacion_incorrecta'][2]['ruta'], '/latest/meta-data')

    def test_un_valor_desconocido_en_la_cabecera_solo_se_cuenta_no_es_una_fuga(self):
        analisis = self.hook.analizar_mensajes([mensaje('GET', '/usuarios', 401, token='carga-de-ataque-en-la-cabecera')], CONFIG, self.TOKENS)
        self.assertEqual(analisis['autorizacion_incorrecta_total'], 0)
        self.assertEqual(analisis['cabecera_authorization_con_valor_desconocido'], 1)

    def test_zap_started_instala_las_barreras_de_neutralizacion_y_de_retirada_de_credencial(self):
        zap = ZapFalso()
        self.hook.zap_started(zap, DESTINO)
        reglas = {p['description']: p for u, p in zap.solicitudes if u.endswith('replacer/action/addRule/')}
        sin = reglas['igualab-sin-credencial']
        self.assertEqual((sin['matchType'], sin['matchString'], sin['replacement']), ('REQ_HEADER', 'Authorization', ''))
        self.assertEqual(sin['url'], CONFIG['url_sin_credencial_regex'])
        n0, n1 = reglas['igualab-neutralizar-0'], reglas['igualab-neutralizar-1']
        for regla in (n0, n1):
            self.assertEqual((regla['matchType'], regla['matchRegex'], regla['replacement']), ('REQ_HEADER_STR', 'true', 'OPTIONS '))
        self.assertEqual(n0['matchString'], '^(POST) ')
        self.assertEqual(n0['url'], CONFIG['neutralizar_metodos'][0]['url_regex'])
        self.assertEqual(self.hook._ESTADO['reglas_de_barrera'], 3)

    def test_el_resultado_de_la_comprobacion_no_contiene_valores_de_tokens(self):
        analisis = self.hook.analizar_mensajes([mensaje('GET', '/reportes', 403, token=TOKEN_SUPER)], CONFIG, self.TOKENS)
        texto = json.dumps(analisis)
        for secreto in (TOKEN_SUPER, TOKEN_ADMIN):
            self.assertNotIn(secreto, texto)

    def test_zap_active_scan_aborta_si_un_token_va_al_rol_equivocado(self):
        zap = ZapFalso(mensajes=[mensaje('GET', '/reportes', 403, token=TOKEN_SUPER)], urls=[DESTINO + '/reportes'])
        with self.assertRaises(RuntimeError):
            self.hook.zap_active_scan(zap, DESTINO, 'Default Policy')
        resumen = json.loads(Path(self.trabajo.name, 'zap-hook-resumen.json').read_text(encoding='utf-8'))
        self.assertEqual(resumen['autorizacion_incorrecta'][0]['rol_esperado'], 'administrador')

    # ------------------------------------------------------------------------------------------------- análisis activo
    def test_sin_escaneo_activo_no_se_considera_ejecutado(self):
        self.assertFalse(self.hook.resumir_analisis_activo([], [])['ejecutado'])
        self.assertFalse(self.hook.resumir_analisis_activo([{'id': '0', 'reqCount': '0', 'progress': '100'}], [])['ejecutado'])

    def test_escaneo_completo_y_parcial(self):
        escaneos = [{'id': '0', 'reqCount': '120', 'progress': '100', 'state': 'FINISHED'}]
        completo = [{'scanProgress': [DESTINO, {'HostProcess': [
            {'Plugin': ['SQL Injection', '40018', 'release', 'Complete', '100', '60', '0']},
            {'Plugin': ['Log4Shell', '40043', 'beta', 'Skipped, technology not detected', '0', '0', '0']},
        ]}]}]
        resumen = self.hook.resumir_analisis_activo(escaneos, completo)
        self.assertTrue(resumen['ejecutado'])
        self.assertTrue(resumen['completo'])
        self.assertEqual(resumen['reglas_omitidas_total'], 1)
        parcial = [{'scanProgress': [DESTINO, {'HostProcess': [
            {'Plugin': ['SQL Injection', '40018', 'release', 'Complete', '100', '60', '0']},
            {'Plugin': ['Path Traversal', '6', 'release', 'Pending', '0', '0', '0']},
        ]}]}]
        resumen = self.hook.resumir_analisis_activo(escaneos, parcial)
        self.assertTrue(resumen['ejecutado'])
        self.assertFalse(resumen['completo'])
        self.assertEqual(resumen['reglas_pendientes_o_interrumpidas'][0]['id'], '6')

    # ------------------------------------------------------------------------------------------------------- antes del ataque
    def test_zap_active_scan_aborta_si_lo_importado_incluye_una_operacion_excluida(self):
        zap = ZapFalso(mensajes=[mensaje('POST', '/auth/logout', 204)], urls=[DESTINO + '/usuarios'])
        with self.assertRaises(RuntimeError):
            self.hook.zap_active_scan(zap, DESTINO, 'Default Policy')
        resumen = json.loads(Path(self.trabajo.name, 'zap-hook-resumen.json').read_text(encoding='utf-8'))
        self.assertTrue(resumen['abortado_antes_del_ataque'])
        self.assertEqual(resumen['violaciones_de_alcance'][0]['ruta'], '/auth/logout')

    def test_zap_active_scan_aborta_si_hay_urls_fuera_del_destino(self):
        zap = ZapFalso(mensajes=[], urls=[DESTINO + '/usuarios', 'https://igualab-uat.ejemplo.pe/usuarios'])
        with self.assertRaises(RuntimeError):
            self.hook.zap_active_scan(zap, DESTINO, 'Default Policy')
        resumen = json.loads(Path(self.trabajo.name, 'zap-hook-resumen.json').read_text(encoding='utf-8'))
        self.assertEqual(resumen['urls_fuera_del_destino'], ['https://igualab-uat.ejemplo.pe/usuarios'])

    def test_zap_active_scan_permite_continuar_si_todo_esta_en_alcance(self):
        zap = ZapFalso(mensajes=[mensaje('GET', '/usuarios', 200)], urls=[DESTINO + '/usuarios'])
        self.assertIsNone(self.hook.zap_active_scan(zap, DESTINO, 'Default Policy'))
        self.assertFalse(Path(self.trabajo.name, 'zap-hook-resumen.json').exists())

    # ---------------------------------------------------------------------------------------------------------- resumen final
    def test_zap_pre_shutdown_escribe_un_resumen_sin_secretos(self):
        mensajes = [mensaje('GET', '/usuarios', 200), mensaje('GET', '/reportes', 200, token=TOKEN_ADMIN)]
        escaneos = [{'id': '0', 'reqCount': '50', 'progress': '100'}]
        progreso = {'scanProgress': [DESTINO, {'HostProcess': [{'Plugin': ['SQL Injection', '40018', 'release', 'Complete', '1', '50', '0']}]}]}
        zap = ZapFalso(mensajes=mensajes, escaneos=escaneos, progreso=progreso)
        self.hook.zap_pre_shutdown(zap)
        texto = Path(self.trabajo.name, 'zap-hook-resumen.json').read_text(encoding='utf-8')
        resumen = json.loads(texto)
        self.assertTrue(resumen['analisis_activo']['ejecutado'])
        self.assertTrue(resumen['cobertura_autenticada']['demostrada'])
        self.assertEqual(resumen['violaciones_total'], 0)
        for secreto in (TOKEN_SUPER, TOKEN_ADMIN, 'Bearer'):
            self.assertNotIn(secreto, texto)

    def test_si_el_resumen_falla_se_deja_constancia_sin_romper_la_salida_de_zap(self):
        class Roto(ZapFalso):
            def __init__(self):
                super().__init__()
                self.core.messages = lambda start='0', count='500': (_ for _ in ()).throw(ConnectionError('x'))

        self.hook.zap_pre_shutdown(Roto())
        resumen = json.loads(Path(self.trabajo.name, 'zap-hook-resumen.json').read_text(encoding='utf-8'))
        self.assertEqual(resumen, {'version': 1, 'error_del_hook': 'ConnectionError'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
