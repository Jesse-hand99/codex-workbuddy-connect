# Launch the Codex desktop app with WorkBuddy overrides.
# Fully quit the desktop app first; otherwise its single-instance behavior
# focuses the existing process and ignores these overrides.
$ErrorActionPreference = 'Stop'

if (Get-Process -Name ChatGPT -ErrorAction SilentlyContinue) {
    Write-Host 'Codex desktop is still running. Fully quit it, then run this script again.' -ForegroundColor Yellow
    exit 1
}

if ($env:WB_KEEP_PROXY -ne '1') {
    foreach ($name in @(
        'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
        'http_proxy', 'https_proxy', 'all_proxy'
    )) {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}
$env:NO_PROXY = 'localhost,127.0.0.1,::1'
$env:no_proxy = 'localhost,127.0.0.1,::1'

$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$catalog = (Join-Path $codexHome 'workbuddy-catalog.json').Replace('\', '/')
$workspace = if ($args.Count -gt 0) { $args[0] } else { (Get-Location).Path }

& codex app $workspace `
    -c 'model_provider="workbuddy"' `
    -c 'model="deepseek-v4.1-flash"' `
    -c 'model_reasoning_effort="low"' `
    -c "model_catalog_json=$catalog" `
    -c 'model_providers.workbuddy.base_url="http://localhost:8787/v1"'
exit $LASTEXITCODE
