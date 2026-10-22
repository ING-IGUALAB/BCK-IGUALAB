"""Hooks de zap-api-scan.py (ZAP 2.17.0) para el backend de Igualab.

Funciones:
  * zap_started        -> ANTES de importar el OpenAPI (la importación envía una petición por operación) inyecta la cabecera
                          Authorization por rol mediante reglas de Replacer con filtro de URL, y excluye del ataque las rutas
                          cuyos métodos están todos excluidos. Los tokens se leen de /zap/privado y nunca se imprimen.
  * zap_active_scan    -> ANTES del ataque verifica que todo lo importado esté en el destino permitido y que no se haya enviado
                          ninguna petición a una operación excluida; si no, aborta (el script oficial termina con código 3).
  * zap_pre_shutdown   -> escribe /zap/wrk/zap-hook-resumen.json con la evidencia: cobertura autenticada por rol y por operación,
                          violaciones de alcance y estado real del análisis activo. No contiene tokens, cabeceras ni cuerpos.

Los mensajes de error son fijos a propósito: el cliente de ZAP envía los parámetros en la URL y una excepción podría contenerlos.
"""
import json
import os
import re
from urllib.parse import urlsplit

DIR_TRABAJO = os.environ.get('ZAP_HOOK_DIR_TRABAJO', '/zap/wrk')
DIR_PRIVADO = os.environ.get('ZAP_HOOK_DIR_PRIVADO', '/zap/privado')
RUTA_CONFIG = os.path.join(DIR_TRABAJO, 'hook-config.json')
RUTA_CREDENCIALES = os.path.join(DIR_PRIVADO, 'credenciales.json')
RUTA_RESUMEN = os.path.join(DIR_TRABAJO, 'zap-hook-resumen.json')

PAGINA = 500
MAX_LISTA = 50
CON_AUTORIZACION = re.compile(r'(?im)^authorization:\s*bearer\s+(\S+)')
LINEA_PETICION = re.compile(r'^([A-Z]+)\s+(\S+)\s+HTTP/')
LINEA_ESTADO = re.compile(r'^HTTP/\S+\s+(\d{3})')
ESTADOS_COMPLETOS = ('complete', 'skipped')

_ESTADO = {'reglas_autenticacion': [], 'reglas_de_barrera': 0, 'exclusiones_de_ataque': 0, 'urls_importadas': None, 'revision_previa': None}


def _cargar(ruta):
    with open(ruta, encoding='utf-8') as archivo:
        return json.load(archivo)


# ---------------------------------------------------------------------------------------------------------------- análisis puro
def _coincide(patron, texto):
    return re.search(patron, texto) is not None


def analizar_mensajes(mensajes, config, tokens=None):
    """Resume los mensajes del historial de ZAP respecto del alcance. No conserva cabeceras ni cuerpos.

    Devuelve: totales, cobertura por rol y por operación, violaciones (operaciones excluidas), orígenes ajenos al destino y,
    si se pasan los `tokens` por rol, las peticiones con una credencial REAL INCORRECTA: token en un host ajeno, en una ruta sin regla
    de autenticación (p. ej. /health, /auth/login o rutas de sondeo) o de otro rol que el que corresponde a la URL. Las cabeceras con
    un valor que no es ninguna credencial de la ejecución solo se cuentan. Los tokens solo se comparan en
    memoria: el resultado contiene roles y rutas, nunca valores.
    """
    destino = config['destino']
    incluidas = config['incluidas']
    excluidas = config['excluidas']
    roles = config['roles']
    por_rol = {rol: {'autenticadas': 0, 'alcanzaron_la_logica': 0, 'exitosas_2xx': 0} for rol in roles}
    por_operacion = {}
    violaciones = []
    violaciones_total = 0
    incorrectas = []
    incorrectas_total = 0
    token_desconocido = 0
    ajenos = set()
    total = 0
    for mensaje in mensajes:
        cabecera = mensaje.get('requestHeader') or ''
        primera = cabecera.splitlines()[0] if cabecera else ''
        coincidencia = LINEA_PETICION.match(primera)
        if not coincidencia:
            continue
        total += 1
        metodo, url = coincidencia.group(1), coincidencia.group(2)
        partes = urlsplit(url)
        origen = f'{partes.scheme}://{partes.netloc}'
        ruta = partes.path or '/'
        coincidencia_token = CON_AUTORIZACION.search(cabecera)
        if tokens is not None and coincidencia_token:
            usado = next((rol for rol, valor in tokens.items() if valor == coincidencia_token.group(1)), 'desconocido')
            esperado = None
            if origen == destino:
                esperado = next((rol for rol, datos in roles.items() if re.search(datos['url_regex'], url)), None)
            if usado == 'desconocido':
                # Valor distinto de las credenciales de la ejecución (p. ej. una carga de ataque en la propia cabecera): no es una fuga.
                token_desconocido += 1
            elif esperado != usado:
                # Una credencial REAL en un host ajeno, en una ruta sin regla de rol o de otro rol: violación.
                incorrectas_total += 1
                if len(incorrectas) < MAX_LISTA:
                    incorrectas.append({'metodo': metodo, 'ruta': ruta if origen == destino else '(host ajeno)', 'rol_esperado': esperado or 'ninguno', 'rol_del_token': usado})
        if origen != destino:
            ajenos.add(origen)
            continue
        estado_http = LINEA_ESTADO.match((mensaje.get('responseHeader') or '').splitlines()[0] if mensaje.get('responseHeader') else '')
        codigo = int(estado_http.group(1)) if estado_http else 0
        con_autorizacion = coincidencia_token is not None

        operacion = next((op for op in incluidas if op['metodo'] == metodo and _coincide(op['regex'], ruta)), None)
        if operacion is None:
            prohibida = next((op for op in excluidas if op['metodo'] == metodo and _coincide(op['regex'], ruta)), None)
            if prohibida is not None:
                violaciones_total += 1
                if len(violaciones) < MAX_LISTA:
                    violaciones.append({'metodo': metodo, 'ruta': prohibida['ruta'], 'estado_http': codigo})
            continue
        clave = f"{operacion['metodo']} {operacion['ruta']}"
        datos = por_operacion.setdefault(clave, {'rol': operacion['rol'], 'peticiones': 0, 'autenticadas': 0, 'alcanzaron_la_logica': 0, 'estados': {}})
        datos['peticiones'] += 1
        if con_autorizacion:
            datos['autenticadas'] += 1
            if codigo not in (0, 401, 403):
                datos['alcanzaron_la_logica'] += 1
            datos['estados'][str(codigo)] = datos['estados'].get(str(codigo), 0) + 1
            rol = operacion['rol']
            if rol in por_rol:
                por_rol[rol]['autenticadas'] += 1
                if codigo not in (0, 401, 403):
                    por_rol[rol]['alcanzaron_la_logica'] += 1
                if 200 <= codigo < 300:
                    por_rol[rol]['exitosas_2xx'] += 1
    return {
        'total_peticiones': total,
        'por_rol': por_rol,
        'por_operacion': por_operacion,
        'violaciones': violaciones,
        'violaciones_total': violaciones_total,
        'origenes_ajenos': sorted(ajenos)[:MAX_LISTA],
        'autorizacion_incorrecta': incorrectas,
        'autorizacion_incorrecta_total': incorrectas_total,
        'cabecera_authorization_con_valor_desconocido': token_desconocido,
    }


def cobertura_autenticada(analisis, config):
    minimos = config.get('minimos_autenticados_por_rol', {})
    demostrada = bool(minimos)
    for rol, minimo in minimos.items():
        datos = analisis['por_rol'].get(rol, {})
        if datos.get('alcanzaron_la_logica', 0) < minimo or datos.get('exitosas_2xx', 0) < 1:
            demostrada = False
    sin_cobertura = sorted(
        clave
        for clave, datos in analisis['por_operacion'].items()
        if datos['rol'] != 'publico' and datos['alcanzaron_la_logica'] == 0
    )
    sin_peticiones = sorted(
        f"{op['metodo']} {op['ruta']}"
        for op in config['incluidas']
        if op['rol'] != 'publico' and f"{op['metodo']} {op['ruta']}" not in analisis['por_operacion']
    )
    return {
        'demostrada': demostrada,
        'por_rol': analisis['por_rol'],
        'operaciones_sin_cobertura_autenticada': sorted(set(sin_cobertura + sin_peticiones)),
    }


def _plugins(progreso):
    """Extrae (nombre, id, estado, peticiones) del árbol que devuelve ascan.scan_progress, sin depender de su forma exacta."""
    encontrados = []

    def visitar(nodo):
        if isinstance(nodo, list):
            if len(nodo) >= 7 and all(isinstance(x, str) for x in nodo[:7]) and nodo[1].isdigit():
                encontrados.append((nodo[0], nodo[1], nodo[3], int(nodo[5]) if nodo[5].isdigit() else 0))
                return
            for hijo in nodo:
                visitar(hijo)
        elif isinstance(nodo, dict):
            if 'Plugin' in nodo and isinstance(nodo['Plugin'], list) and len(nodo['Plugin']) >= 7:
                visitar(nodo['Plugin'])
                return
            for hijo in nodo.values():
                visitar(hijo)

    visitar(progreso)
    return encontrados


def resumir_analisis_activo(escaneos, progresos):
    """Estado REAL del análisis activo. Un escaneo con -S (o sin peticiones de ataque) no cuenta como ejecutado."""
    escaneos = [e for e in (escaneos or []) if isinstance(e, dict)]
    plugins = [p for progreso in progresos for p in _plugins(progreso)]
    peticiones = sum(int(e.get('reqCount', 0) or 0) for e in escaneos) or sum(p[3] for p in plugins)
    ejecutado = bool(escaneos) and peticiones > 0
    pendientes = [p for p in plugins if not p[2].lower().startswith(ESTADOS_COMPLETOS)]
    progreso_menor = [e for e in escaneos if str(e.get('progress', '100')).isdigit() and int(e['progress']) < 100]
    completo = None
    if ejecutado:
        completo = not pendientes and not progreso_menor and bool(plugins)
    omitidas = [{'regla': p[0], 'id': p[1], 'estado': p[2][:80]} for p in plugins if p[2].lower().startswith('skipped')]
    return {
        'ejecutado': ejecutado,
        'completo': completo,
        'escaneos': len(escaneos),
        'peticiones_de_ataque': peticiones,
        'reglas_ejecutadas': len([p for p in plugins if p[2].lower().startswith('complete')]),
        'reglas_pendientes_o_interrumpidas': [{'regla': p[0], 'id': p[1], 'estado': p[2][:80]} for p in pendientes][:MAX_LISTA],
        'reglas_omitidas_por_zap': omitidas[:MAX_LISTA],
        'reglas_omitidas_total': len(omitidas),
    }


# ----------------------------------------------------------------------------------------------------------------- acceso a ZAP
def _todos_los_mensajes(zap):
    inicio = 0
    while True:
        pagina = zap.core.messages(start=str(inicio), count=str(PAGINA))
        if not pagina:
            return
        for mensaje in pagina:
            yield mensaje
        if len(pagina) < PAGINA:
            return
        inicio += PAGINA


def _escribir_resumen(datos):
    temporal = RUTA_RESUMEN + '.tmp'
    with open(temporal, 'w', encoding='utf-8') as archivo:
        json.dump(datos, archivo, ensure_ascii=False, indent=2)
    os.replace(temporal, RUTA_RESUMEN)


def _agregar_regla_generica(zap, descripcion, tipo, es_regex, coincidencia, reemplazo, url_regex):
    respuesta = zap._request(zap.base + 'replacer/action/addRule/', {
        'description': descripcion,
        'enabled': 'true',
        'matchType': tipo,
        'matchRegex': es_regex,
        'matchString': coincidencia,
        'replacement': reemplazo,
        'url': url_regex,
    })
    if not isinstance(respuesta, dict) or respuesta.get('Result') != 'OK':
        raise RuntimeError('replacer rechazo la regla')


def _agregar_regla(zap, descripcion, url_regex, token):
    _agregar_regla_generica(zap, descripcion, 'REQ_HEADER', 'false', 'Authorization', 'Bearer ' + token, url_regex)


# ---------------------------------------------------------------------------------------------------------------------- hooks
def zap_started(zap, target):
    try:
        config = _cargar(RUTA_CONFIG)
        credenciales = _cargar(RUTA_CREDENCIALES)
        for rol, datos in config['roles'].items():
            token = credenciales.get(rol)
            if not token:
                raise RuntimeError('falta la credencial de un rol')
            _agregar_regla(zap, 'igualab-autenticacion-' + rol, datos['url_regex'], token)
            _ESTADO['reglas_autenticacion'].append(rol)
        # Barreras que IMPIDEN (la detección posterior es solo la segunda línea): ninguna credencial fuera de las rutas de un rol y
        # ninguna operación excluida con su método real (se reescribe a OPTIONS, que el backend rechaza con 405 sin ejecutar nada).
        if config.get('url_sin_credencial_regex'):
            _agregar_regla_generica(zap, 'igualab-sin-credencial', 'REQ_HEADER', 'false', 'Authorization', '', config['url_sin_credencial_regex'])
            _ESTADO['reglas_de_barrera'] += 1
        for indice, regla in enumerate(config.get('neutralizar_metodos', [])):
            _agregar_regla_generica(zap, f'igualab-neutralizar-{indice}', 'REQ_HEADER_STR', 'true', regla['metodo_regex'], 'OPTIONS ', regla['url_regex'])
            _ESTADO['reglas_de_barrera'] += 1
        for patron in config.get('excluir_del_ataque', []):
            zap.ascan.exclude_from_scan(patron)
            zap.spider.exclude_from_scan(patron)
            _ESTADO['exclusiones_de_ataque'] += 1
    except Exception:
        raise RuntimeError('hook: no se pudo configurar la autenticacion ni las exclusiones') from None


def zap_active_scan(zap, target, policy):
    """Se ejecuta antes del ataque (decorador @hook de zap_common): aquí ya se importó la especificación."""
    try:
        config = _cargar(RUTA_CONFIG)
        tokens = _cargar(RUTA_CREDENCIALES)
        urls = zap.core.urls()
        _ESTADO['urls_importadas'] = len(urls)
        fuera = sorted({u for u in urls if urlsplit(u).scheme + '://' + urlsplit(u).netloc != config['destino']})
        analisis = analizar_mensajes(_todos_los_mensajes(zap), config, tokens)
        _ESTADO['revision_previa'] = {
            'urls_fuera_del_destino': fuera[:MAX_LISTA],
            'violaciones_de_alcance': analisis['violaciones'],
            'origenes_ajenos': analisis['origenes_ajenos'],
            'autorizacion_incorrecta': analisis['autorizacion_incorrecta'],
        }
    except Exception:
        raise RuntimeError('hook: no se pudo verificar el alcance antes del ataque') from None
    if fuera or analisis['violaciones_total'] or analisis['origenes_ajenos'] or analisis['autorizacion_incorrecta_total']:
        _escribir_resumen({'abortado_antes_del_ataque': True, **_ESTADO['revision_previa']})
        raise RuntimeError('hook: el alcance importado no es el permitido; se aborta antes del ataque activo')


def zap_pre_shutdown(zap):
    try:
        config = _cargar(RUTA_CONFIG)
        analisis = analizar_mensajes(_todos_los_mensajes(zap), config, _cargar(RUTA_CREDENCIALES))
        escaneos = zap.ascan.scans
        progresos = []
        for escaneo in escaneos or []:
            try:
                progresos.append(zap.ascan.scan_progress(escaneo.get('id')))
            except Exception:
                progresos.append(None)
        previa = _ESTADO.get('revision_previa') or {}
        _escribir_resumen({
            'version': 1,
            'reglas_de_autenticacion': _ESTADO['reglas_autenticacion'],
            'exclusiones_de_ataque': _ESTADO['exclusiones_de_ataque'],
            'reglas_de_barrera': _ESTADO['reglas_de_barrera'],
            'cabecera_authorization_con_valor_desconocido': analisis['cabecera_authorization_con_valor_desconocido'],
            'urls_importadas': _ESTADO['urls_importadas'],
            'urls_fuera_del_destino': previa.get('urls_fuera_del_destino', []) + analisis['origenes_ajenos'],
            'violaciones_de_alcance': analisis['violaciones'],
            'violaciones_total': analisis['violaciones_total'],
            'total_peticiones': analisis['total_peticiones'],
            'autorizacion_incorrecta': analisis['autorizacion_incorrecta'],
            'autorizacion_incorrecta_total': analisis['autorizacion_incorrecta_total'],
            'analisis_activo': resumir_analisis_activo(escaneos, progresos),
            'cobertura_autenticada': cobertura_autenticada(analisis, config),
            'por_operacion': analisis['por_operacion'],
        })
    except Exception as error:
        _escribir_resumen({'version': 1, 'error_del_hook': type(error).__name__})
