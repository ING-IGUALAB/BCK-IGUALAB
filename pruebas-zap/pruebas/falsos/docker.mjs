/**
 * `docker` FALSO para verificar ejecutar-zap.sh en local aislado (se antepone al PATH). No es Docker: simula solo los subcomandos
 * que usa el script y ejecuta preparar/finalizar con Node local. Registra cada acción en $FALSO_ESTADO/acciones.log para que las
 * pruebas comprueben, por ejemplo, que al agotarse el tiempo se eliminó ÚNICAMENTE el contenedor de ZAP de la ejecución.
 */
import { spawn, spawnSync } from 'node:child_process';
import { appendFileSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const aqui = dirname(fileURLToPath(import.meta.url));
const RAIZ_ZAP = join(aqui, '..', '..');
const ESTADO = process.env.FALSO_ESTADO;
mkdirSync(ESTADO, { recursive: true });
const accion = (texto) => appendFileSync(join(ESTADO, 'acciones.log'), `${texto}\n`);
const archivoContenedor = (nombre) => join(ESTADO, `contenedor-${nombre}.json`);
const leer = (ruta) => JSON.parse(readFileSync(ruta, 'utf8'));

/** MSYS (Git Bash en Windows) entrega /c/Users/...: se convierte a una ruta que Node entienda. */
const anfitrion = (ruta) => (process.platform === 'win32' ? ruta.replace(/^\/([a-zA-Z])\//, '$1:/') : ruta);

const [subcomando, ...args] = process.argv.slice(2);
accion(`${subcomando} ${args.filter((a) => !a.startsWith('-e')).join(' ').replace(/\s+/g, ' ').slice(0, 2000)}`);

function formatoDe(args) {
  const i = args.indexOf('-f');
  return i >= 0 ? args[i + 1] : '';
}

if (subcomando === 'build') process.exit(process.env.FALSO_BUILD_FALLA ? 1 : 0);
if (subcomando === 'pull') process.exit(process.env.FALSO_PULL_FALLA ? 1 : 0);
if (subcomando === 'network') process.exit(process.env.FALSO_SIN_RED ? 1 : 0);

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
  const ejecutando = (() => { try { process.kill(c.pid, 0); return !existsSync(join(ESTADO, `salida-${nombre}`)); } catch { return false; } })();
  if (formato.includes('.State.Running')) console.log(String(ejecutando));
  else if (formato.includes('.State.ExitCode')) console.log(existsSync(join(ESTADO, `salida-${nombre}`)) ? readFileSync(join(ESTADO, `salida-${nombre}`), 'utf8').trim() : '137');
  else if (formato.includes('igualab.zap.ejecucion')) console.log(c.etiqueta);
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
    if (forzar) { try { process.kill(c.pid, 'SIGKILL'); } catch { /* ya terminó */ } }
    writeFileSync(join(ESTADO, `salida-${nombre}`), existsSync(join(ESTADO, `salida-${nombre}`)) ? readFileSync(join(ESTADO, `salida-${nombre}`), 'utf8') : '137');
    rmSync(archivoContenedor(nombre), { force: true });
    accion(`REMOVIDO ${nombre}`);
  }
  process.exit(0);
}

if (subcomando === 'run') {
  const o = { montajes: [], etiquetas: [], entorno: [], segundoPlano: false, nombre: null, entrypoint: null };
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
      if (bandera === '-v') { const [h, c] = valor.replace(/:(rw|ro)$/, '').split(/:(?=\/[^/])/); o.montajes.push([anfitrion(h), c]); }
    }
    i += 1;
  }
  const imagen = args[i];
  const comando = args.slice(i + 1);
  const traducir = (texto) => o.montajes.reduce((t, [h, c]) => (t === c || t.startsWith(`${c}/`) ? h + t.slice(c.length) : t), texto);

  if (imagen === 'igualab-pruebas-zap:ci') {
    const [, script, ...resto] = comando; // node scripts/X.mjs …
    const r = spawnSync(process.execPath, [join(RAIZ_ZAP, script), ...resto.map(traducir)], { stdio: 'inherit', env: process.env });
    process.exit(r.status ?? 3);
  }
  // Imagen de ZAP
  if (o.entrypoint) process.exit(0); // chmod de normalización
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
