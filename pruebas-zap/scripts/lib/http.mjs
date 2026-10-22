/**
 * Cliente HTTP mínimo. Nunca registra cuerpos, cabeceras ni tokens: devuelve el estado y el cuerpo ya interpretado para quien llama.
 */
export async function pedir(baseUrl, metodo, ruta, { token, json, plazoMs = 20_000 } = {}) {
  const cabeceras = { accept: 'application/json' };
  if (token) cabeceras.authorization = `Bearer ${token}`;
  if (json !== undefined) cabeceras['content-type'] = 'application/json';
  const respuesta = await fetch(`${baseUrl}${ruta}`, {
    method: metodo,
    headers: cabeceras,
    body: json === undefined ? undefined : JSON.stringify(json),
    signal: AbortSignal.timeout(plazoMs),
    redirect: 'manual',
  });
  const texto = await respuesta.text();
  let cuerpo = null;
  try {
    cuerpo = JSON.parse(texto);
  } catch {
    cuerpo = null;
  }
  return { estado: respuesta.status, cuerpo, texto, tipo: respuesta.headers.get('content-type') ?? '' };
}

export const dormir = (ms) => new Promise((resolver) => setTimeout(resolver, ms));

/** Clasifica un fallo de red sin volcar la excepción completa. */
export function describirFallo(error) {
  return (
    error?.cause?.code ?? error?.cause?.errors?.find((e) => e?.code)?.code ?? error?.code ?? error?.name ?? 'ERROR'
  );
}
