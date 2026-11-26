# install-skill.ps1
# Copies the adobe-dimension skill into your personal Claude Code skills folder (~/.claude/skills).
# Purely additive: it creates skills\adobe-dimension\ and touches nothing else.
# Run from the repo folder:
#   powershell -ExecutionPolicy Bypass -File .\install-skill.ps1
# Add -Force to replace an existing copy.
param([switch]$Force)
$ErrorActionPreference = "Stop"

$Source = Join-Path $PSScriptRoot "skill\adobe-dimension"
$Root   = Join-Path $env:USERPROFILE ".claude\skills"
$Dest   = Join-Path $Root "adobe-dimension"

if (-not (Test-Path (Join-Path $Source "SKILL.md"))) { Write-Host "ERROR: $Source\SKILL.md not found" -ForegroundColor Red; exit 1 }
New-Item -ItemType Directory -Force $Root | Out-Null

if (Test-Path $Dest) {
    if (-not $Force) { Write-Host "$Dest already exists. Re-run with -Force to replace it." -ForegroundColor Yellow; exit 0 }
    Remove-Item -Recurse -Force $Dest
}
Copy-Item -Path $Source -Destination $Dest -Recurse
if (Test-Path (Join-Path $Dest "SKILL.md")) {
    Write-Host "Installed: $Dest" -ForegroundColor Green
    Write-Host "Start a new Claude Code session to load it. For claude.ai / Claude Desktop chat, zip the"
    Write-Host "skill\adobe-dimension folder and upload it under Settings > Capabilities > Skills."
} else { Write-Host "Install failed." -ForegroundColor Red; exit 1 }
