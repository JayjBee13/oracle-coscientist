[CmdletBinding()]
param(
    [string]$ClaudeCredentials = (Join-Path $env:USERPROFILE ".claude\.credentials.json"),
    [string]$ClaudeState = (Join-Path $env:USERPROFILE ".claude.json"),
    [string]$CodexCredentials = (Join-Path $env:USERPROFILE ".codex\auth.json")
)

$ErrorActionPreference = "Stop"
$composeRoot = Split-Path -Parent $PSScriptRoot

function Assert-NativeSuccess([string]$Action) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed with exit code $LASTEXITCODE."
    }
}

function Read-Json([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not found at $Path"
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function Write-ContainerSecret([string]$Json, [string]$Destination) {
    $writer = "import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(sys.stdin.read(), encoding='utf-8'); os.chmod(p, 0o600)"
    $Json | & docker compose run --rm --no-deps -T --entrypoint python app -c $writer $Destination
    Assert-NativeSuccess "writing $Destination"
}

Push-Location $composeRoot
try {
    $claude = Read-Json $ClaudeCredentials "Claude credentials"
    if (-not $claude.claudeAiOauth) {
        throw "Claude credentials do not contain claudeAiOauth."
    }
    $safeClaude = [ordered]@{ claudeAiOauth = $claude.claudeAiOauth }
    Write-ContainerSecret ($safeClaude | ConvertTo-Json -Depth 100 -Compress) "/home/oracle/.claude/.credentials.json"

    if (Test-Path -LiteralPath $ClaudeState -PathType Leaf) {
        $state = Read-Json $ClaudeState "Claude onboarding state"
        $safeState = [ordered]@{}
        foreach ($name in @("hasCompletedOnboarding", "lastOnboardingVersion")) {
            $property = $state.PSObject.Properties[$name]
            if ($null -ne $property) {
                $safeState[$name] = $property.Value
            }
        }
        if ($safeState.Count -gt 0) {
            Write-ContainerSecret ($safeState | ConvertTo-Json -Depth 10 -Compress) "/home/oracle/.claude.json"
        }
    }

    $codex = Read-Json $CodexCredentials "Codex credentials"
    $safeCodex = [ordered]@{}
    foreach ($name in @("auth_mode", "tokens", "last_refresh")) {
        $property = $codex.PSObject.Properties[$name]
        if ($null -ne $property) {
            $safeCodex[$name] = $property.Value
        }
    }
    if (-not $safeCodex.tokens) {
        throw "Codex credentials do not contain subscription OAuth tokens."
    }
    Write-ContainerSecret ($safeCodex | ConvertTo-Json -Depth 100 -Compress) "/home/oracle/.codex/auth.json"
    Write-ContainerSecret 'cli_auth_credentials_store = "file"' "/home/oracle/.codex/config.toml"

    Write-Host "Seeded the Oracle credential volume with scoped Claude and Codex OAuth state."
}
finally {
    Pop-Location
}
