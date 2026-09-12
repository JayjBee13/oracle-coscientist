# Safety doctor. Read-only diagnostic: verifies the seven hard safety rules from the
# overhaul plan's Global Constraints (DB isolation, migration state, archive integrity,
# historical-source immutability, no orphaned supervisors, no leaked secrets) and reports
# the harness gate configuration. It does not launch Claude or Codex, and it never mutates
# any run row or file — only tasklist.exe (read-only) and SELECT queries.
#
# Exit code is non-zero iff any of checks 1-6 FAIL. Check 7 is informational and cannot fail.

$ErrorActionPreference = "Stop"

$guiRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$backendRoot = Join-Path $guiRoot "backend"
$python = Join-Path $backendRoot ".venv\Scripts\python.exe"
$envFile = Join-Path $guiRoot ".env"

if (-not (Test-Path $python)) {
  throw "Backend virtualenv not found at $python"
}

Write-Output "Safety doctor."
Write-Output "Verifies DB isolation, migration state, archive integrity, historical-source"
Write-Output "immutability, orphaned supervisors, and secret-free spawn logs. Read-only."
Write-Output ""

$checkStatus = @{}
$anyFailed = $false

# ------------------------------------------------------------------------------------------
# Checks 1, 2, 4, 5: database identity, alembic head, historical source roots, orphan
# supervisors. Bundled into one Python process so they share a single DB connection and can
# reuse the app's own settings/store/archive_data code instead of reimplementing it here.
# ------------------------------------------------------------------------------------------

Write-Output "== Checks 1/2/4/5: database identity, alembic head, source roots, orphan supervisors =="

$doctorScript = @'
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine
from sqlalchemy import text as sql_text

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import get_settings

failures = 0


def report(status: str, name: str, message: str) -> None:
    global failures
    print(f"{status} [{name}] {message}")
    if status == "FAIL":
        failures += 1


settings = get_settings()
engine = create_engine(settings.database_url)

# ---------------------------------------------------------------- check 1: db identity
with engine.connect() as conn:
    db_name, db_user = conn.execute(sql_text("select current_database(), current_user")).one()
ok = db_name == "ai_coscientist_gui" and db_user == "ai_coscientist_gui_app"
report(
    "PASS" if ok else "FAIL",
    "db-identity",
    f"database={db_name} user={db_user} "
    "(expected ai_coscientist_gui / ai_coscientist_gui_app, from .env)",
)

# ---------------------------------------------------------------- check 2: alembic head
head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
with engine.connect() as conn:
    current = conn.execute(sql_text("select version_num from alembic_version")).scalar_one()
report("PASS" if current == head else "FAIL", "alembic-head", f"current={current} head={head}")

# ---------------------------------------------------------- check 4: historical source roots
# Reuses archive_data.py's own hashing/manifest code rather than reimplementing it here: for
# every file still sitting in the two historical, read-only source roots, its hash must match
# the byte-exact copy already archived under archive (Global Constraint: those
# roots are read-only until Task 6.3's gated deletion).
backend_root = Path.cwd()
gui_root = backend_root.parent
scripts_dir = gui_root / "scripts"
sys.path.insert(0, str(scripts_dir))
import archive_data as ad  # noqa: E402

required, _optional = ad.read_manifest()
manifest_index = {path: (digest, size) for path, digest, size in required}

SOURCE_CHECKS = tuple(
    (label, ad.PROJECT_ROOT / rel, f"imported-runs/{label}")
    for label, rel in ad.RUN_SOURCES
    if label in ("v1", "v2")
)

drift: list[str] = []
skipped: list[str] = []
checked_files = 0
for label, source_root, prefix in SOURCE_CHECKS:
    if not source_root.exists():
        skipped.append(label)
        continue
    for path in ad.iter_files(source_root):
        rel = path.relative_to(source_root).as_posix()
        entry = manifest_index.get(f"{prefix}/{rel}")
        checked_files += 1
        if entry is None:
            drift.append(f"{label}:{rel} (not archived)")
            continue
        digest, size = entry
        if path.stat().st_size != size:
            drift.append(f"{label}:{rel} (size changed)")
            continue
        if ad.sha256_of(path) != digest:
            drift.append(f"{label}:{rel} (hash changed)")

if len(skipped) == len(SOURCE_CHECKS):
    print(
        "SKIP [source-roots] both historical source roots are gone "
        "(ai-coscientist/runs, ai-coscientist_v2/runs) - cleanup has run, nothing to compare"
    )
elif drift:
    shown = "; ".join(drift[:8]) + (f"; ... +{len(drift) - 8} more" if len(drift) > 8 else "")
    report("FAIL", "source-roots", f"{len(drift)} file(s) drifted from the archive: {shown}")
else:
    note = f", skipped absent root(s): {','.join(skipped)}" if skipped else ""
    report(
        "PASS",
        "source-roots",
        f"{checked_files} file(s) match archive/MANIFEST.sha256 byte-for-byte{note}",
    )

# --------------------------------------------------------------- check 5: orphan supervisors
# A run row holding its harness lane (LANE_LIFECYCLES) with a recorded supervisor_pid must
# either have a live, recognisable supervisor process behind that pid AND a heartbeat fresher
# than STALE_HEARTBEAT_SECONDS, or it is an orphan: a lane held by nothing. This mirrors the
# reconciler's own alive/fresh test (services/runs/controls.py) without its side effect of
# writing lifecycle='lost' - the doctor only reports, it never mutates.
from app.db.engine_models import LANE_LIFECYCLES
from app.engine.spawn import process_image
from app.engine.store import RunStore
from app.services.runs.controls import STALE_HEARTBEAT_SECONDS, SUPERVISOR_IMAGES

store = RunStore(settings=settings)
active = store.active_runs(lifecycles=LANE_LIFECYCLES)
candidates = [r for r in active if r.get("supervisor_pid")]


def _parse_ts(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


now = datetime.now(timezone.utc)
orphans: list[str] = []
for run in candidates:
    pid = int(run["supervisor_pid"])
    heartbeat = _parse_ts(run.get("heartbeat_at"))
    age = (now - heartbeat).total_seconds() if heartbeat else None
    fresh = age is not None and age < STALE_HEARTBEAT_SECONDS
    image = process_image(pid)
    alive = image is not None and image.lower() in SUPERVISOR_IMAGES
    if not (alive and fresh):
        age_desc = f"{age:.0f}s" if age is not None else "no heartbeat"
        orphans.append(
            f"{run['engine_run_id']} (lifecycle={run['lifecycle']} pid={pid} "
            f"alive={alive} heartbeat_age={age_desc})"
        )

if orphans:
    report("FAIL", "orphan-supervisors", f"{len(orphans)} orphan(s): " + "; ".join(orphans))
else:
    report(
        "PASS",
        "orphan-supervisors",
        f"no orphans ({len(candidates)} run(s) with a recorded supervisor_pid, "
        f"{len(active)} active-lane row(s) total)",
    )

# ------------------------------------------------------------------------------- check 7 data
# Informational only - gathered here because it shares the already-open DB connection.
counts_rows = None
with engine.connect() as conn:
    counts_rows = conn.execute(
        sql_text(
            "select lifecycle, count(*) from runs where deleted_at is null "
            "group by lifecycle order by 1"
        )
    ).all()
counts_desc = ", ".join(f"{lifecycle}={count}" for lifecycle, count in counts_rows) or "no runs"
print(f"REPORT [runs-by-lifecycle] {counts_desc}")
print(f"REPORT [real-harness-enabled] {str(settings.real_harness_enabled).lower()}")
print(f"REPORT [fake-harness-enabled] {str(settings.fake_harness_enabled).lower()}")
engine_runner = os.environ.get("ENGINE_RUNNER")
print(f"REPORT [engine-runner-env] {engine_runner if engine_runner else '(unset)'}")

sys.exit(1 if failures else 0)
'@

Push-Location $backendRoot
try {
  $pyOutput = $doctorScript | & $python - 2>&1
}
finally {
  Pop-Location
}
$pyOutput | ForEach-Object { Write-Output $_ }

foreach ($name in @("db-identity", "alembic-head", "source-roots", "orphan-supervisors")) {
  $line = $pyOutput | Where-Object { $_ -match "^(PASS|FAIL|SKIP)\s+\[$name\]" } | Select-Object -First 1
  if ($line -and ($line -match "^(PASS|FAIL|SKIP)\s+\[$name\]")) {
    $checkStatus[$name] = $Matches[1]
    if ($Matches[1] -eq "FAIL") { $anyFailed = $true }
  }
  else {
    $checkStatus[$name] = "FAIL"
    $anyFailed = $true
    Write-Output "FAIL [$name] no result line found in safety-doctor python output (it may have crashed early - see output above)"
  }
}
Write-Output ""

# ------------------------------------------------------------------------------------------
# Check 3: archive integrity. Delegates entirely to archive_data.py verify (byte-exact re-hash
# of the whole archive against MANIFEST.sha256) rather than reimplementing hashing here.
# ------------------------------------------------------------------------------------------

Write-Output "== Check 3: archive integrity =="

$archiveOutput = & $python (Join-Path $guiRoot "scripts\archive_data.py") verify 2>&1
$archiveExit = $LASTEXITCODE
$archiveOutput | ForEach-Object { Write-Output $_ }

$archiveOkLine = $archiveOutput | Where-Object { $_ -match "ARCHIVE OK (\d+) files" } | Select-Object -First 1
if ($archiveExit -eq 0 -and $archiveOkLine -and ($archiveOkLine -match "ARCHIVE OK (\d+) files")) {
  Write-Output "PASS [archive-integrity] archive_data.py verify -> ARCHIVE OK $($Matches[1]) files"
  $checkStatus["archive-integrity"] = "PASS"
}
else {
  Write-Output "FAIL [archive-integrity] archive_data.py verify did not report ARCHIVE OK (exit=$archiveExit) - see output above"
  $checkStatus["archive-integrity"] = "FAIL"
  $anyFailed = $true
}
Write-Output ""

# ------------------------------------------------------------------------------------------
# Check 6: no secrets in recorded spawn argv. Scans every spawn-argv.jsonl under the runs
# root (one per run directory, written by the role-call spawn gate) for any argv element
# that looks like a DSN/URL or a bare credential - the Global Constraint is that secrets
# reach the harness via environment/Settings only, never argv.
# ------------------------------------------------------------------------------------------

Write-Output "== Check 6: no secrets in recorded spawn argv =="

$runsRootOverride = $null
if (Test-Path $envFile) {
  Get-Content $envFile | ForEach-Object {
    if ($_ -match '^\s*COSCIENTIST_RUNS_ROOT\s*=\s*(.+?)\s*$') {
      $runsRootOverride = $Matches[1]
    }
  }
}
if ([string]::IsNullOrWhiteSpace($runsRootOverride)) {
  $runsRoot = Join-Path $guiRoot "engines\runs"
}
elseif ([System.IO.Path]::IsPathRooted($runsRootOverride)) {
  $runsRoot = $runsRootOverride
}
else {
  $runsRoot = Join-Path $guiRoot $runsRootOverride
}

$argvFiles = @()
if (Test-Path $runsRoot) {
  $argvFiles = @(Get-ChildItem -Path $runsRoot -Filter "spawn-argv.jsonl" -Recurse -File -ErrorAction SilentlyContinue)
}

$secretHits = New-Object System.Collections.Generic.List[string]
$linesScanned = 0
$credentialPattern = '(?i)(api[_-]?key|token|secret|password|passwd|authorization|bearer)\s*[:=]'

foreach ($file in $argvFiles) {
  $lineNumber = 0
  foreach ($rawLine in Get-Content -LiteralPath $file.FullName) {
    $lineNumber++
    if ([string]::IsNullOrWhiteSpace($rawLine)) { continue }
    $linesScanned++
    try {
      $entry = $rawLine | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
      $secretHits.Add("$($file.FullName):${lineNumber} could not parse JSON line")
      continue
    }
    foreach ($token in @($entry.argv)) {
      if ($null -eq $token) { continue }
      $tokenText = [string]$token
      if ($tokenText -match "://") {
        $secretHits.Add("$($file.FullName):${lineNumber} argv element contains '://' -> $tokenText")
      }
      elseif ($tokenText -match $credentialPattern) {
        $secretHits.Add("$($file.FullName):${lineNumber} argv element looks like a credential -> $tokenText")
      }
    }
  }
}

if ($secretHits.Count -gt 0) {
  Write-Output "FAIL [spawn-argv-secrets] $($secretHits.Count) suspicious argv element(s) across $($argvFiles.Count) file(s):"
  $secretHits | ForEach-Object { Write-Output "  $_" }
  $checkStatus["spawn-argv-secrets"] = "FAIL"
  $anyFailed = $true
}
else {
  Write-Output "PASS [spawn-argv-secrets] $linesScanned line(s) across $($argvFiles.Count) spawn-argv.jsonl file(s) under $runsRoot, no secrets found"
  $checkStatus["spawn-argv-secrets"] = "PASS"
}
Write-Output ""

# ------------------------------------------------------------------------------------------
# Check 7: report (informational only - never fails).
# ------------------------------------------------------------------------------------------

Write-Output "== Check 7: report (informational) =="
$pyOutput | Where-Object { $_ -match "^REPORT " } | ForEach-Object { Write-Output $_ }

foreach ($command in @("claude", "codex")) {
  $resolved = Get-Command $command -ErrorAction SilentlyContinue
  if ($null -eq $resolved) {
    Write-Output "REPORT [harness-cli] ${command}: not found"
    continue
  }
  $version = & $command --version 2>&1
  Write-Output "REPORT [harness-cli] ${command}: $version"
}
$checkStatus["report"] = "PASS"
Write-Output ""

# ------------------------------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------------------------------

Write-Output "== Summary =="
$order = @("db-identity", "alembic-head", "archive-integrity", "source-roots", "orphan-supervisors", "spawn-argv-secrets", "report")
$passed = 0
$skipped = 0
$failed = 0
foreach ($name in $order) {
  $status = $checkStatus[$name]
  if (-not $status) {
    $status = "FAIL"
    $checkStatus[$name] = "FAIL"
    $anyFailed = $true
    Write-Output "FAIL [$name] check did not report a result"
  }
  switch ($status) {
    "PASS" { $passed++ }
    "SKIP" { $skipped++ }
    "FAIL" { $failed++ }
  }
}

$summaryParts = New-Object System.Collections.Generic.List[string]
$summaryParts.Add("$passed passed")
if ($skipped -gt 0) { $summaryParts.Add("$skipped skipped") }
if ($failed -gt 0) { $summaryParts.Add("$failed failed") }
Write-Output "SAFETY DOCTOR: $($order.Count) checks, $($summaryParts -join ', ')"

if ($anyFailed) {
  exit 1
}
exit 0
