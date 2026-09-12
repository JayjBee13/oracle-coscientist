$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$backendRoot = Join-Path $repoRoot "backend"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
  throw "Backend virtualenv not found at $python"
}

Push-Location $backendRoot
try {
  & $python -m pytest tests/unit/test_artifact_parser.py tests/integration/test_runs_api.py -q
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}
finally {
  Pop-Location
}
