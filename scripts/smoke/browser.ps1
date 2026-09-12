<#
  End-to-end browser smoke: the real backend, the real frontend, a real browser.

  Boots both servers on free ports, walks the whole product against them
  (imported history, a demo run launched through the wizard and watched live,
  compare, and the backend-down error state), and fails on a single browser
  console error.

  Its backend runs against a throwaway `smoke_<8hex>` schema, created and
  dropped by `_smoke_schema.py`, because the walkthrough asserts the archived
  historical runs render and importing those into the owner's live schema is
  not something a smoke gets to do.

  Nothing here calls Claude or Codex: the run it launches is a demo run, which
  the engine services with the scripted FakeRunner. It creates exactly the runs
  it soft-deletes on the way out.
#>

$ErrorActionPreference = "Stop"

function Get-FreeTcpPort {
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), 0)
  $listener.Start()
  try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}

function Wait-Http {
  param(
    [string] $Uri,
    [int] $TimeoutSec = 60,
    [System.Diagnostics.Process[]] $Processes = @()
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    foreach ($process in $Processes) {
      if ($process -and $process.HasExited) {
        throw "Process $($process.Id) exited before $Uri became ready"
      }
    }
    try {
      $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3
      if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) { return }
    }
    catch { Start-Sleep -Milliseconds 400 }
  }
  throw "Timed out waiting for $Uri"
}

function Stop-ProcessTree {
  param([int] $ProcessId)

  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
  foreach ($child in $children) { Stop-ProcessTree -ProcessId $child.ProcessId }
  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

# --- paths ------------------------------------------------------------------

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$frontendRoot = Join-Path $repoRoot "frontend"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
$flowTemplate = Join-Path $PSScriptRoot "browser_flow.js"
$schemaHelper = Join-Path $PSScriptRoot "_smoke_schema.py"
$shotDir = Join-Path $repoRoot "output\playwright"
$workDir = Join-Path ([System.IO.Path]::GetTempPath()) "coscientist-browser-smoke-$([Guid]::NewGuid().ToString('N'))"

if (-not (Get-Command npx -ErrorAction SilentlyContinue)) { throw "npx was not found on PATH" }
if (-not (Test-Path $python)) { throw "Backend virtualenv not found at $python" }
if (-not (Test-Path (Join-Path $frontendRoot "package.json"))) { throw "Frontend package.json not found" }
if (-not (Test-Path $flowTemplate)) { throw "Browser flow not found at $flowTemplate" }
if (-not (Test-Path $schemaHelper)) { throw "Schema helper not found at $schemaHelper" }

if (Test-Path $shotDir) { Remove-Item (Join-Path $shotDir "*.png") -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $shotDir | Out-Null
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

# --- configuration ----------------------------------------------------------

$backendPort = Get-FreeTcpPort
$frontendPort = Get-FreeTcpPort
$backendUrl = "http://127.0.0.1:$backendPort"
$frontendUrl = "http://127.0.0.1:$frontendPort"
$apiBase = "$backendUrl/api"
$session = "coscientist-smoke-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"

# A demo call is instant, which makes a demo run finish before anybody could
# watch it. This paces one so the live view has intermediate states to show.
$demoLatency = "1.5"
$expectedImportedRuns = 15

$env:APP_ENV = "smoke"
$env:APP_HOST = "127.0.0.1"
$env:APP_PORT = "$backendPort"
$env:FRONTEND_ORIGIN = $frontendUrl
$env:REAL_HARNESS_ENABLED = "false"
$env:ENGINE_RUNNER = "fake"
$env:COSCIENTIST_DEMO_LATENCY = $demoLatency
$env:VITE_API_BASE_URL = $apiBase

# `DATABASE_URL` and `IMPORT_ON_STARTUP` are set per-run once the throwaway schema
# exists, and put back on the way out: leaving `IMPORT_ON_STARTUP=true` behind in the
# caller's session with the live DSN restored is exactly how the archived runs would
# end up back in `public`.
$previousDatabaseUrl = $env:DATABASE_URL
$previousImportOnStartup = $env:IMPORT_ON_STARTUP

$backendProcess = $null
$frontendProcess = $null
$smokeSchema = $null
$createdRunIds = @()
$phaseResults = [ordered]@{}

function Start-Backend {
  $process = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$backendPort") `
    -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru
  Wait-Http -Uri "$apiBase/health" -TimeoutSec 90 -Processes @($process)
  return $process
}

function Invoke-Phase {
  param([string] $Phase)

  $config = [ordered]@{
    phase                 = $Phase
    baseUrl               = $frontendUrl
    apiBase               = $apiBase
    shotDir               = $shotDir.Replace("\", "/")
    timeoutMs             = 30000
    expectedImportedRuns  = $expectedImportedRuns
  } | ConvertTo-Json -Compress

  $flowFile = Join-Path $workDir "flow-$Phase.js"
  (Get-Content $flowTemplate -Raw -Encoding UTF8).Replace("__SMOKE_CONFIG__", $config) |
    Set-Content -Path $flowFile -Encoding UTF8

  Write-Output "  -> $Phase"
  $output = & npx --yes --package "@playwright/cli" playwright-cli "-s=$session" run-code --filename $flowFile --raw 2>&1
  $text = ($output | Out-String).Trim()

  if ($LASTEXITCODE -ne 0 -or $text -match "### Error" -or $text -match "^Error:") {
    throw "browser phase '$Phase' failed:`n$text"
  }

  try { $parsed = $text | ConvertFrom-Json } catch { $parsed = $null }
  if ($null -eq $parsed) { throw "browser phase '$Phase' returned no result:`n$text" }
  if ($parsed.consoleErrors -gt 0) { throw "browser phase '$Phase' saw console errors" }

  $phaseResults[$Phase] = $parsed
  if ($parsed.demoRunIds) { $script:createdRunIds = @($parsed.demoRunIds) }
  return $parsed
}

Write-Output "Browser smoke."
Write-Output "Backend $backendUrl  frontend $frontendUrl  demo latency ${demoLatency}s"

try {
  # The walkthrough expects the archived runs to be there, so this backend boots with
  # the importer on — against a schema of its own, never the owner's.
  $dsn = & $python $schemaHelper create
  if ($LASTEXITCODE -ne 0) { throw "could not create a throwaway schema" }
  $dsn = $dsn | Where-Object { $_ -match "^postgresql" } | Select-Object -Last 1
  if ($dsn -notmatch "(smoke_[0-9a-f]{8})") { throw "the schema helper printed no usable DATABASE_URL" }
  $smokeSchema = $Matches[1]
  $env:DATABASE_URL = $dsn
  $env:IMPORT_ON_STARTUP = "true"
  Write-Output "Schema $smokeSchema (throwaway; dropped on the way out)"

  $backendProcess = Start-Backend

  # A demo run left running by an earlier smoke holds the demo lane. Clear only
  # demo runs: a real run of the owner's may legitimately be in flight.
  $stale = Invoke-RestMethod -Uri "$apiBase/runs?harness=demo&include_demo=true&page_size=100" -TimeoutSec 20
  foreach ($run in $stale.items) {
    if ($run.lifecycle -notin @("completed", "failed", "stopped", "lost")) {
      Write-Output "  clearing stale demo run $($run.id) ($($run.lifecycle))"
      try {
        Invoke-RestMethod -Method Post -Uri "$apiBase/runs/$($run.id)/controls" `
          -ContentType "application/json" -Body '{"action":"force_stop"}' -TimeoutSec 20 | Out-Null
      }
      catch { }
    }
    try { Invoke-RestMethod -Method Delete -Uri "$apiBase/runs/$($run.id)" -TimeoutSec 20 | Out-Null } catch { }
  }

  $frontendProcess = Start-Process -FilePath "npm.cmd" `
    -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "$frontendPort", "--strictPort") `
    -WorkingDirectory $frontendRoot -WindowStyle Hidden -PassThru
  Wait-Http -Uri $frontendUrl -TimeoutSec 120 -Processes @($frontendProcess)

  & npx --yes --package "@playwright/cli" playwright-cli "-s=$session" open $frontendUrl | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "could not open the browser session" }

  Invoke-Phase "boot"       | Out-Null
  Invoke-Phase "imported"   | Out-Null
  Invoke-Phase "hypothesis" | Out-Null
  Invoke-Phase "no_report"  | Out-Null
  Invoke-Phase "wizard"     | Out-Null
  Invoke-Phase "live"       | Out-Null
  Invoke-Phase "finished"   | Out-Null
  Invoke-Phase "compare"    | Out-Null

  # The error state is only honest if the backend is genuinely gone.
  Write-Output "  -- stopping the backend --"
  Stop-ProcessTree -ProcessId $backendProcess.Id
  $backendProcess.WaitForExit(15000) | Out-Null
  $backendProcess = $null

  Invoke-Phase "offline" | Out-Null

  Write-Output "  -- restarting the backend --"
  $backendProcess = Start-Backend

  Invoke-Phase "recover" | Out-Null

  Write-Output ""
  Write-Output "Browser smoke OK."
  foreach ($key in $phaseResults.Keys) {
    $value = $phaseResults[$key] | ConvertTo-Json -Compress -Depth 4
    Write-Output "  $key $value"
  }
  $shots = Get-ChildItem $shotDir -Filter *.png | Sort-Object Name
  Write-Output "  screenshots ($($shots.Count)): $(($shots | ForEach-Object { $_.Name }) -join ', ')"
}
finally {
  & npx --yes --package "@playwright/cli" playwright-cli "-s=$session" close 2>$null | Out-Null

  # Soft-delete what the smoke created, while its backend is still up.
  if ($createdRunIds.Count -gt 0) {
    if (-not $backendProcess -or $backendProcess.HasExited) {
      try { $backendProcess = Start-Backend } catch { }
    }
    foreach ($runId in $createdRunIds) {
      try {
        Invoke-RestMethod -Method Post -Uri "$apiBase/runs/$runId/controls" `
          -ContentType "application/json" -Body '{"action":"force_stop"}' -TimeoutSec 20 | Out-Null
      }
      catch { }
      try {
        Invoke-RestMethod -Method Delete -Uri "$apiBase/runs/$runId" -TimeoutSec 20 | Out-Null
        Write-Output "  soft-deleted demo run $runId"
      }
      catch { Write-Warning "could not soft-delete $runId : $_" }
    }
  }

  if ($frontendProcess -and -not $frontendProcess.HasExited) { Stop-ProcessTree -ProcessId $frontendProcess.Id }
  if ($backendProcess -and -not $backendProcess.HasExited) { Stop-ProcessTree -ProcessId $backendProcess.Id }

  # Once the servers are down nothing else is holding the schema, and no later command
  # in this session should inherit either the dead DSN or the importer flag.
  if ($smokeSchema) {
    if ($null -eq $previousDatabaseUrl) { Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue }
    else { $env:DATABASE_URL = $previousDatabaseUrl }
    if ($null -eq $previousImportOnStartup) { Remove-Item Env:IMPORT_ON_STARTUP -ErrorAction SilentlyContinue }
    else { $env:IMPORT_ON_STARTUP = $previousImportOnStartup }

    & $python $schemaHelper drop $smokeSchema
    if ($LASTEXITCODE -ne 0) { Write-Warning "could not drop schema $smokeSchema" }
  }

  $resolvedWork = [System.IO.Path]::GetFullPath($workDir)
  $temporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
  if (-not $resolvedWork.StartsWith($temporaryRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
      [System.IO.Path]::GetFileName($resolvedWork) -notmatch '^coscientist-browser-smoke-[0-9a-f]{32}$') {
    throw "Refusing cleanup outside the smoke's temporary directory"
  }
  Remove-Item -LiteralPath $resolvedWork -Recurse -Force -ErrorAction SilentlyContinue
}
