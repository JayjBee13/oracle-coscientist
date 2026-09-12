<#
  The guarded real-harness smoke: one small research run, through the real API, the real
  supervisor and the real Claude CLI. It is the only script in this repository that spends
  the owner's quota, and the only one that can prove the product works.

  It refuses to run without ALLOW_REAL_HARNESS_SMOKE=YES in the environment. That is the
  whole guard: nothing here is discoverable by accident, nothing runs it on a schedule, and
  `fake_full_stack.ps1` - the gate everyone else runs - deliberately does not call it.

  What it costs: one workshop call plus one run of at most 14 model calls, capped at $6.00
  and 25 minutes by the run's own config, with both ceilings enforced inside the engine as
  well as here. A clean run measures about $4 and ten minutes; see the note beside CONFIG
  in the driver for where the ceiling came from.

  The assertions live in `real_tiny_smoke.py` beside this file; this script owns the guard,
  a backend on a free port, and making sure that whatever happens, no supervisor is left
  running and the server is stopped.
#>

param(
  [ValidateRange(120, 3600)]
  [int] $DeadlineSeconds = 1500,

  [switch] $SkipWorkshop,

  # Rehearse this script and its driver against the scripted runner. Spends nothing and
  # proves nothing about the product - the grounding, telemetry and cache checks cannot
  # pass on a fake - but a typo in the driver is then found for free rather than a third
  # of the way through a run that cost real quota.
  [switch] $Demo
)

$ErrorActionPreference = "Stop"

# --- the guard --------------------------------------------------------------

if (-not $Demo -and $env:ALLOW_REAL_HARNESS_SMOKE -ne "YES") {
  throw "Refusing to spend real quota. Set ALLOW_REAL_HARNESS_SMOKE=YES for this process to run one bounded real Claude smoke."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
$driver = Join-Path $PSScriptRoot "real_tiny_smoke.py"

if (-not (Test-Path $python)) { throw "Backend virtualenv not found at $python" }
if (-not (Test-Path $driver)) { throw "Smoke driver not found at $driver" }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { throw "The claude CLI was not found on PATH" }

# --- helpers ----------------------------------------------------------------

function Get-FreeTcpPort {
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), 0)
  $listener.Start()
  try { return $listener.LocalEndpoint.Port } finally { $listener.Stop() }
}

function Wait-Http {
  param([string] $Uri, [int] $TimeoutSec = 90, [System.Diagnostics.Process] $Process)

  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    if ($Process -and $Process.HasExited) { throw "The backend exited before $Uri became ready" }
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

# --- configuration ----------------------------------------------------------

$backendPort = Get-FreeTcpPort
$apiBase = "http://127.0.0.1:$backendPort"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outDir = Join-Path $repoRoot "output\real-smoke"
$evidence = Join-Path $outDir "evidence-$stamp.json"
$transcript = Join-Path $outDir "transcript-$stamp.txt"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$env:APP_ENV = "real-smoke"
$env:APP_HOST = "127.0.0.1"
$env:APP_PORT = "$backendPort"
$env:REAL_HARNESS_ENABLED = "true"
# Explicit rather than merely unset: this is the one run that must not be served by the
# fake, and an ENGINE_RUNNER left over from a browser smoke in the same shell would.
$env:ENGINE_RUNNER = if ($Demo) { "fake" } else { "claude" }
# The archive is already imported; re-walking 582 files would only slow the boot.
$env:IMPORT_ON_STARTUP = "false"
$env:PYTHONUTF8 = "1"

Write-Output $(if ($Demo) { "Real tiny smoke - DEMO REHEARSAL (scripted runner, spends nothing)." } else { "Real tiny smoke." })
Write-Output "One workshop call and one 1-round run: 14 calls max, `$6.00 max, $([math]::Round($DeadlineSeconds / 60)) min max."
Write-Output "Backend $apiBase  evidence $evidence"
Write-Output ""

$backend = $null
$exitCode = 1

try {
  $backend = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$backendPort") `
    -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru
  Wait-Http -Uri "$apiBase/api/health" -TimeoutSec 120 -Process $backend

  # -u because the driver's progress lines are the only view of a run that takes twenty
  # minutes, and a pipe makes Python block-buffer them until the very end.
  $arguments = @("-u", $driver, "--api-base", $apiBase, "--deadline-seconds", "$DeadlineSeconds", "--json-out", $evidence)
  if ($SkipWorkshop) { $arguments += "--skip-workshop" }
  if ($Demo) { $arguments += "--demo" }

  Push-Location $backendRoot
  try {
    & $python @arguments 2>&1 | Tee-Object -FilePath $transcript
    $exitCode = $LASTEXITCODE
  }
  finally { Pop-Location }
}
finally {
  # Whatever happened above, no run of ours may outlive this script holding the claude lane
  # and spending money. Ask the API first (cooperative, still writes a report), then kill.
  if ($backend -and -not $backend.HasExited) {
    try {
      $harness = if ($Demo) { "demo" } else { "claude" }
      $active = Invoke-RestMethod -Uri "$apiBase/api/runs?harness=$harness&include_demo=true&page_size=25" -TimeoutSec 20
      foreach ($run in $active.items) {
        # Only ever this smoke's own runs: the owner may legitimately have one in flight,
        # and force-stopping somebody else's research is not this script's business.
        if ($run.title -notlike "Real smoke*") { continue }
        if ($run.lifecycle -in @("queued", "running", "pausing", "paused", "stopping", "finishing")) {
          Write-Warning "run $($run.id) is still $($run.lifecycle); force-stopping it"
          try {
            Invoke-RestMethod -Method Post -Uri "$apiBase/api/runs/$($run.id)/controls" `
              -ContentType "application/json" -Body '{"action":"force_stop"}' -TimeoutSec 30 | Out-Null
          }
          catch { Write-Warning "force stop failed: $_" }
        }
      }
    }
    catch { Write-Warning "could not check for active runs: $_" }

    Stop-ProcessTree -ProcessId $backend.Id
  }
}

Write-Output ""
Write-Output "Transcript: $transcript"
if ($exitCode -eq 0) {
  Write-Output "REAL TINY SMOKE PASSED."
}
else {
  Write-Output "REAL TINY SMOKE FAILED (exit $exitCode). The evidence file and transcript above are the diagnosis."
}
exit $exitCode
