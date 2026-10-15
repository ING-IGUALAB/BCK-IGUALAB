/**
 * Espera la disponibilidad del backend tras el despliegue con un PLAZO ACOTADO (no un sleep fijo) y diagnóstico claro.
 */
import { SALIDA, ErrorDeEjecucion } from './configuracion.mjs';

const dormir = (ms) => new Promise((resolver) => setTimeout(resolver, ms));

/** Clasifica un fallo de red sin volcar la excepción completa (ni cabeceras ni cuerpos). */
function describirFallo(error) {
  // `fetch` envuelve la causa real (a veces en un AggregateError de IPv4/IPv6): se busca el código en cada nivel.
  const codigo =
    error?.cause?.code ?? error?.cause?.errors?.find((e) => e?.code)?.code ?? error?.code ??
    (error?.cause?.message ? `${error.name}: ${String(error.cause.message).slice(0, 120)}` : (error?.name ?? 'ERROR'));
  if (/ECONNREFUSED/.test(codigo)) return `ECONNREFUSED — ${'conexión rechazada: el contenedor aún no escucha en ese puerto'}`;
  const pistas = {
    ECONNREFUSED: 'conexión rechazada: el contenedor aún no escucha en ese puerto',
    ENOTFOUND: 'el nombre del host no se resuelve: ¿la red de Docker (proxy_net) o IGUALAB_API_URL son correctas?',
    EAI_AGAIN: 'el DNS no respondió: ¿la red de Docker es la correcta?',
    ETIMEDOUT: 'tiempo de conexión agotado: ¿hay una ruta de red hasta el backend?',
    UND_ERR_CONNECT_TIMEOUT: 'tiempo de conexión agotado: ¿hay una ruta de red hasta el backend?',
    TimeoutError: 'el backend no respondió a tiempo',
    AbortError: 'el backend no respondió a tiempo',
  };
  return `${codigo}${pistas[codigo] ? ` — ${pistas[codigo]}` : ''}`;
}

export async function esperarBackend({ baseUrl, esperaSegundos, registrar, intervaloMs = 3000, intentoMs = 5000 }) {
  const limite = Date.now() + esperaSegundos * 1000;
  let intentos = 0;
  let ultimo = 'sin intentos';
  let ultimoImpreso = '';
  const inicio = Date.now();
  while (true) {
    intentos += 1;
    try {
      const respuesta = await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(intentoMs), redirect: 'manual' });
      if (respuesta.ok) {
        const cuerpo = await respuesta.json().catch(() => null);
        if (cuerpo?.status === 'ok') {
          registrar(`Backend disponible tras ${intentos} intento(s) y ${((Date.now() - inicio) / 1000).toFixed(1)} s (GET /health → 200 ok).`);
          return;
        }
        ultimo = 'GET /health → 200 pero con un cuerpo distinto de {"status":"ok"}';
      } else {
        ultimo = `GET /health → HTTP ${respuesta.status}`;
      }
    } catch (error) {
      ultimo = describirFallo(error);
    }
    if (ultimo !== ultimoImpreso || intentos % 5 === 0) {
      registrar(`Esperando al backend (intento ${intentos}, ${Math.round((Date.now() - inicio) / 1000)} s de ${esperaSegundos} s): ${ultimo}`);
      ultimoImpreso = ultimo;
    }
    if (Date.now() + intervaloMs >= limite) break;
    await dormir(intervaloMs);
  }
  throw new ErrorDeEjecucion(
    `El backend no estuvo disponible en ${esperaSegundos} s (${intentos} intentos). Último diagnóstico: ${ultimo}. ` +
      'Revise que el despliegue haya terminado, que el contenedor esté sano (docker ps) y que IGUALAB_API_URL sea alcanzable desde esta red.',
    SALIDA.BACKEND_NO_DISPONIBLE,
  );
}
