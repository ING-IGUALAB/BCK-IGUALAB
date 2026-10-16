<#
.SYNOPSIS
  Ejecuta las colecciones contra el entorno local AISLADO (proyecto Compose `igualab-local`) usando la misma imagen que Jenkins.

.DESCRIPTION
  Lee el SuperAdmin local de `entorno-local\generado\backend.env` (archivo ignorado por Git) SIN mostrarlo y lo entrega al
  contenedor por variables de entorno. Los reportes quedan en `pruebas-http\resultados\<coleccion>`.
  Las carpetas B y C usan el OCI real que tenga configurado el backend local (muestra mínima, sin reintentos): no las ejecute
  sin autorización. La colección de integración y los flujos A y D no usan OCI.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\pruebas-http\scripts\ejecutar-local.ps1 -Coleccion integracion
  powershell -NoProfile -ExecutionPolicy Bypass -File .\pruebas-http\scripts\ejecutar-local.ps1 -Coleccion e2e -Carpeta A,D
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('integracion', 'e2e')][string]$Coleccion,
  [string[]]$Carpeta = @(),
  [string]$Red = 'igualab-local_default',
  [string]$UrlApi = 'http://backend:8000'
)
$ErrorActionPreference = 'Stop'
$raiz = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$archivoEnv = Join-Path $raiz 'entorno-local\generado\backend.env'
if (-not (Test-Path $archivoEnv)) { throw "No existe $archivoEnv. Inicie el entorno local (entorno-local\iniciar.ps1)." }

function Leer-Valor([string]$nombre) {
  $linea = Select-String -Path $archivoEnv -Pattern "^$nombre=" | Select-Object -First 1
  if (-not $linea) { throw "Falta $nombre en el archivo del entorno local." }
  return $linea.Line.Substring($nombre.Length + 1).Trim()
}

$imagen = 'igualab-pruebas-http:local'
docker build -t $imagen (Join-Path $raiz 'pruebas-http') | Out-Null
$resultados = Join-Path $raiz 'pruebas-http\resultados'
New-Item -ItemType Directory -Force -Path $resultados | Out-Null

# Los secretos viajan como variables de entorno del proceso (no como argumentos visibles en la lista de procesos).
$env:IGUALAB_E2E_SUPERADMIN_CORREO = Leer-Valor 'SUPERADMIN_CORREO'
$env:IGUALAB_E2E_SUPERADMIN_PASSWORD = Leer-Valor 'SUPERADMIN_PASSWORD'
$argumentos = @('--coleccion', $Coleccion, '--resultados', '/resultados')
foreach ($c in $Carpeta) { $argumentos += @('--carpeta', $c) }

docker run --rm --network $Red `
  -e IGUALAB_PRUEBAS_ENTORNO=local -e "IGUALAB_API_URL=$UrlApi" `
  -e IGUALAB_E2E_SUPERADMIN_CORREO -e IGUALAB_E2E_SUPERADMIN_PASSWORD `
  -v "${resultados}:/resultados" `
  $imagen node /opt/pruebas-http/scripts/ejecutar.mjs @argumentos
$salida = $LASTEXITCODE
Remove-Item Env:IGUALAB_E2E_SUPERADMIN_CORREO, Env:IGUALAB_E2E_SUPERADMIN_PASSWORD -ErrorAction SilentlyContinue
exit $salida
