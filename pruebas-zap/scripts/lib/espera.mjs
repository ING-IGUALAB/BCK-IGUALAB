/**
 * Espera la disponibilidad del backend con un PLAZO ACOTADO (no un sleep fijo) y diagnóstico claro. Mismo criterio que pruebas-http.
 */
import { ErrorDeEjecucion, SALIDA } from './codigos.mjs';
import { describirFallo, dormir } from './http.mjs';

export async function esperarBackend({ baseUrl, esperaSegundos, registrar, intervaloMs = 3000, intentoMs = 5000 }) {
  const limite = Date.now() + esperaSegundos * 1000;
  const inicio = Date.now();
  let intentos = 0;
  let ultimo = 'sin intentos';
  let ultimoImpreso = '';
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
        ultimo = 'GET /health → 200 con un cuerpo distinto de {"status":"ok"}';
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
      'Revise el despliegue de QA, que el contenedor esté sano y que IGUALAB_API_URL sea alcanzable desde proxy_net.',
    SALIDA.BACKEND_NO_DISPONIBLE,
  );
}
