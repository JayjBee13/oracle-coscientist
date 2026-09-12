<#
  The gate. Everything that must be true before this app is worth running.

  Order matters: the cheap checks that explain a failure in one line come
  first, and the browser smoke — which boots both servers and drives a real
  browser for a couple of minutes — comes last, once there is reason to believe
  it can pass.

  Nothing here calls Claude or Codex. The only run it launches is a demo run,
  served by the scripted FakeRunner, and it deletes it on the way out. The
  guarded real-harness smoke is `real_tiny_smoke.ps1` and is not part of this.
#>

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$backendRoot = Join-Path $repoRoot "backend"
$frontendRoot = Join-Path $repoRoot "frontend"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) { throw "Backend virtualenv not found at $python" }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npm was not found on PATH" }

$steps = [System.Collections.Generic.List[object]]::new()
$failed = $null

function Invoke-Step {
  param(
    [string] $Name,
    [string] $WorkingDirectory,
    [scriptblock] $Body
  )

  if ($script:failed) { return }

  Write-Output ""
  Write-Output "== $Name =="
  $started = Get-Date
  Push-Location $WorkingDirectory
  try {
    & $Body
    $code = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
  }
  catch {
    Write-Output $_.Exception.Message
    $code = 1
  }
  finally { Pop-Location }

  $seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
  $script:steps.Add([pscustomobject]@{ Name = $Name; Code = $code; Seconds = $seconds })
  if ($code -ne 0) { $script:failed = $Name }
}

Write-Output "Full-stack gate."
Write-Output "Backend suites, ruff, frontend lint/tests/build/typegen, then a real browser walkthrough."
Write-Output "No Claude or Codex calls: the run it launches is a demo run."

Invoke-Step "safety doctor" $repoRoot { & (Join-Path $PSScriptRoot "safety_doctor.ps1") }

Invoke-Step "backend tests" $backendRoot { & $python -m pytest tests/unit tests/integration -q }

# `ruff check` only. pyproject configures lint rules and nothing else — the
# backend has never been ruff-formatted, and a gate that demands it would
# rewrite a third of the tree the first time anyone ran it.
Invoke-Step "ruff" $backendRoot { & $python -m ruff check . }

Invoke-Step "frontend lint" $frontendRoot { & npm.cmd run lint }

Invoke-Step "frontend tests" $frontendRoot { & npm.cmd run test -- --run }

Invoke-Step "frontend build" $frontendRoot { & npm.cmd run build }

Invoke-Step "openapi typegen" $frontendRoot { & npm.cmd run gen:api:check }

Invoke-Step "browser smoke" $repoRoot { & (Join-Path $PSScriptRoot "browser.ps1") }

Write-Output ""
Write-Output "== summary =="
foreach ($step in $steps) {
  $verdict = if ($step.Code -eq 0) { "PASS" } else { "FAIL" }
  Write-Output ("  {0,-4} {1,-20} {2,6}s" -f $verdict, $step.Name, $step.Seconds)
}

if ($failed) {
  $skipped = $steps.Count
  Write-Output ""
  Write-Output "FAILED at '$failed' (step $skipped of 8). Nothing after it ran."
  exit 1
}

Write-Output ""
Write-Output "All 8 steps passed."
exit 0
