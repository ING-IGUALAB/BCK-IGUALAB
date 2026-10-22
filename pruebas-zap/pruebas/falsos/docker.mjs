/**
 * `docker` FALSO para verificar ejecutar-zap.sh en local aislado (se antepone al PATH). No es Docker: simula solo los subcomandos
 * que usa el script (incluidos los volúmenes nombrados, `create` y `cp`) y ejecuta preparar/finalizar con Node local. Los volúmenes son
 * carpetas bajo $FALSO_ESTADO/volumenes/. Registra cada acción en $FALSO_ESTADO/acciones.log para que las pruebas comprueben, por
 * ejemplo, que al agotarse el tiempo se eliminó ÚNICAMENTE el contenedor de ZAP de la ejecución y los recursos con su etiqueta.
 * NO emula permisos: los casos de propietario/permisos se prueban con Docker real (pruebas/permisos-real.sh).
 */
import { spawn, spawnSync } from 'node:child_process';
import { appendFileSync, cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const aqui = dirname(fileURLToPath(import.meta.url));
const RAIZ_ZAP = join(aqui, '..', '..');
const ESTADO = process.env.FALSO_ESTADO;
mkdirSync(join(ESTADO, 'volumenes'), { recursive: true });
const accion = (texto) => appendFileSync(join(ESTADO, 'acciones.log'), `${texto}\n`);
const archivoContenedor = (nombre) => join(ESTADO, `contenedor-${nombre}.json`);
const dirVolumen = (nombre) => join(ESTADO, 'volumenes', nombre);
const leer = (ruta) => JSON.parse(readFileSync(ruta, 'utf8'));

/** MSYS (Git Bash en Windows) entrega /c/Users/...: se convierte a una ruta que Node entienda. */
const anfitrion = (ruta) => (process.platform === 'win32' ? ruta.replace(/^\/([a-zA-Z])\//, '$1:/') : ruta);

const [subcomando, ...args] = process.argv.slice(2);
accion(`${subcomando} ${args.filter((a) => !a.startsWith('-e')).join(' ').replace(/\s+/g, ' ').slice(0, 2000)}`);

function formatoDe(lista) {
  const i = lista.indexOf('-f');
  if (i >= 0) return lista[i + 1];
  const j = lista.indexOf('--format');
  return j >= 0 ? lista[j + 1] : '';
}

/** `-v origen:destino[:modo]`: un origen sin barra es un VOLUMEN nombrado; si no, una carpeta del anfitrión. */
function analizarMontaje(valor) {
  const [origen, destino] = valor.replace(/:(rw|ro)$/, '').split(/:(?=\/[^/])/);
  if (!/[\\/]/.test(origen)) {
    mkdirSync(dirVolumen(origen), { recursive: true });
    return [dirVolumen(origen), destino, origen];
  }
  return [anfitrion(origen), destino, null];
}

if (subcomando === 'build') process.exit(process.env.FALSO_BUILD_FALLA ? 1 : 0);
if (subcomando === 'pull') process.exit(process.env.FALSO_PULL_FALLA ? 1 : 0);
if (subcomando === 'network') process.exit(process.env.FALSO_SIN_RED ? 1 : 0);
if (subcomando === 'info') {
  console.log('seguridad=[name=fake] · SO=docker-falso');
  process.exit(0);
}

if (subcomando === 'volume') {
  const [accionVolumen, ...resto] = args;
  const nombre = resto.at(-1);
  if (accionVolumen === 'create') {
    if (process.env.FALSO_VOLUMEN_FALLA) process.exit(1);
    const etiqueta = resto.find((x) => x.startsWith('igualab.zap.ejecucion='))?.split('=')[1] ?? '';
    mkdirSync(dirVolumen(nombre), { recursive: true });
    writeFileSync(join(ESTADO, `etiqueta-volumen-${nombre}`), etiqueta);
    process.exit(0);
  }
  if (accionVolumen === 'inspect') {
    if (!existsSync(dirVolumen(nombre))) process.exit(1);
    const f = join(ESTADO, `etiqueta-volumen-${nombre}`);
    console.log(existsSync(f) ? readFileSync(f, 'utf8') : '');
    process.exit(0);
  }
  if (accionVolumen === 'rm') {
    rmSync(dirVolumen(nombre), { recursive: true, force: true });
    rmSync(join(ESTADO, `etiqueta-volumen-${nombre}`), { force: true });
    accion(`VOLUMEN-ELIMINADO ${nombre}`);
    process.exit(0);
  }
  process.exit(0);
}

if (subcomando === 'create') {
  // Contenedor de datos (no se arranca): solo registra sus montajes para `docker cp`.
  let nombre = null;
  let etiqueta = '';
  const montajes = {};
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--name') nombre = args[(i += 1)];
    else if (args[i] === '--label') { etiqueta = args[(i += 1)].split('=')[1] ?? ''; }
    else if (args[i] === '-v') { const [dir, destino] = analizarMontaje(args[(i += 1)]); montajes[destino] = dir; }
    else if (args[i] === '-u') i += 1;
  }
  writeFileSync(archivoContenedor(nombre), JSON.stringify({ tipo: 'datos', etiqueta, montajes }));
  console.log('abcdef0123456789');
  process.exit(0);
}

/** Resuelve una ruta del contenedor de datos (p. ej. /resultados/x) a su carpeta real en el volumen. */
function rutaDeDatos(nombre, rutaContenedor) {
  const c = leer(archivoContenedor(nombre));
  for (const [destino, dir] of Object.entries(c.montajes)) {
    if (rutaContenedor === destino || rutaContenedor.startsWith(`${destino}/`)) return join(dir, rutaContenedor.slice(destino.length));
  }
  throw new Error(`ruta fuera de los montajes: ${rutaContenedor}`);
}

if (subcomando === 'cp') {
  const [origen, destino] = args.filter((a) => !a.startsWith('-'));
  try {
    if (/^igualab-zap-datos-[^:]+:/.test(origen)) {
      const [nombre, ruta] = origen.split(/:(.+)/);
      const real = rutaDeDatos(nombre, ruta.replace(/\/\.$/, ''));
      mkdirSync(anfitrion(destino), { recursive: true });
      cpSync(real, anfitrion(destino), { recursive: true });
    } else {
      const [nombre, ruta] = destino.split(/:(.+)/);
      cpSync(anfitrion(origen), rutaDeDatos(nombre, ruta));
    }
  } catch {
    process.exit(1);
  }
  process.exit(0);
}

if (subcomando === 'inspect') {
  const nombre = args.at(-1);
  const formato = formatoDe(args);
  if (process.env.FALSO_CONTENEDOR_BACKEND && nombre === process.env.FALSO_CONTENEDOR_BACKEND) {
    if (formato.includes('com.docker.compose.project')) console.log(process.env.FALSO_PROYECTO_BACKEND ?? 'igualab-backend-qa');
    else if (formato.includes('Networks')) console.log(process.env.FALSO_REDES_BACKEND ?? 'proxy_net ');
    process.exit(0);
  }
  if (!existsSync(archivoContenedor(nombre))) process.exit(1);
  const c = leer(archivoContenedor(nombre));
  if (formato.includes('igualab.zap.ejecucion')) { console.log(c.etiqueta); process.exit(0); }
  const ejecutando = (() => { try { process.kill(c.pid, 0); return !existsSync(join(ESTADO, `salida-${nombre}`)); } catch { return false; } })();
  if (formato.includes('.State.Running')) console.log(String(ejecutando));
  else if (formato.includes('.State.ExitCode')) console.log(existsSync(join(ESTADO, `salida-${nombre}`)) ? readFileSync(join(ESTADO, `salida-${nombre}`), 'utf8').trim() : '137');
  process.exit(0);
}

if (subcomando === 'logs') {
  const nombre = args.at(-1);
  const seguir = args.includes('-f');
  const ruta = join(ESTADO, `log-${nombre}.txt`);
  let impreso = 0;
  const volcar = () => { if (existsSync(ruta)) { const t = readFileSync(ruta, 'utf8'); process.stdout.write(t.slice(impreso)); impreso = t.length; } };
  const activo = () => existsSync(archivoContenedor(nombre)) && !existsSync(join(ESTADO, `salida-${nombre}`));
  volcar();
  if (!seguir) process.exit(0);
  const temporizador = setInterval(() => { volcar(); if (!activo()) { volcar(); clearInterval(temporizador); process.exit(0); } }, 100);
}

if (subcomando === 'rm') {
  const nombre = args.at(-1);
  const forzar = args.includes('-f');
  if (existsSync(archivoContenedor(nombre))) {
    const c = leer(archivoContenedor(nombre));
    if (c.tipo === 'datos') {
      rmSync(archivoContenedor(nombre), { force: true });
      accion(`DATOS-ELIMINADO ${nombre}`);
      process.exit(0);
    }
    if (forzar) { try { process.kill(c.pid, 'SIGKILL'); } catch { /* ya terminó */ } }
    writeFileSync(join(ESTADO, `salida-${nombre}`), existsSync(join(ESTADO, `salida-${nombre}`)) ? readFileSync(join(ESTADO, `salida-${nombre}`), 'utf8') : '137');
    rmSync(archivoContenedor(nombre), { force: true });
    accion(`REMOVIDO ${nombre}`);
  }
  process.exit(0);
}

if (subcomando === 'run') {
  const o = { montajes: [], etiquetas: [], entorno: [], segundoPlano: false, nombre: null, entrypoint: null, usuario: null };
  let i = 0;
  const conValor = new Set(['--network', '-u', '--name', '--label', '-v', '--entrypoint', '-e']);
  while (i < args.length && args[i].startsWith('-')) {
    const bandera = args[i];
    if (bandera === '-d') o.segundoPlano = true;
    else if (conValor.has(bandera)) {
      const valor = args[i + 1];
      i += 1;
      if (bandera === '--name') o.nombre = valor;
      if (bandera === '--label') o.etiquetas.push(valor);
      if (bandera === '--entrypoint') o.entrypoint = valor;
      if (bandera === '-u') o.usuario = valor;
      if (bandera === '-v') { const [dir, destino] = analizarMontaje(valor); o.montajes.push([dir, destino]); }
    }
    i += 1;
  }
  const imagen = args[i];
  const comando = args.slice(i + 1);
  const traducir = (texto) => o.montajes.reduce((t, [h, c]) => (t === c || t.startsWith(`${c}/`) ? h + t.slice(c.length) : t), texto);

  if (imagen === 'igualab-pruebas-zap:ci') {
    // El orquestador debe usar siempre un usuario no root fijo (nunca el de Jenkins ni root).
    if (o.usuario !== '1000:1000') { console.error(`docker-falso: se esperaba -u 1000:1000 y llegó ${o.usuario}`); process.exit(125); }
    const [, script, ...resto] = comando; // node scripts/X.mjs …
    const r = spawnSync(process.execPath, [join(RAIZ_ZAP, script), ...resto.map(traducir)], { stdio: 'inherit', env: process.env });
    process.exit(r.status ?? 3);
  }
  // Imagen de ZAP
  if (o.entrypoint) process.exit(0);
  const nombre = o.nombre;
  const trabajo = o.montajes.find(([, c]) => c === '/zap/wrk')[0];
  const privado = o.montajes.find(([, c]) => c === '/zap/privado')[0];
  const etiqueta = o.etiquetas.find((e) => e.startsWith('igualab.zap.ejecucion='))?.split('=')[1];
  const hijo = spawn(process.execPath, [join(aqui, 'zap-falso.mjs'), nombre, trabajo, privado, ESTADO], { detached: true, stdio: 'ignore', env: process.env });
  hijo.unref();
  writeFileSync(archivoContenedor(nombre), JSON.stringify({ pid: hijo.pid, etiqueta }));
  writeFileSync(join(ESTADO, `log-${nombre}.txt`), '');
  console.log('0123456789abcdef');
  process.exit(0);
}
if (!['logs'].includes(subcomando)) process.exit(0);
