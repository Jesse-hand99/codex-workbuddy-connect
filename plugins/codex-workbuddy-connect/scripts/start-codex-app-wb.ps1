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

function Resolve-CodexCli {
    if ($env:CODEX_WB_CODEX -and (Test-Path -LiteralPath $env:CODEX_WB_CODEX -PathType Leaf)) {
        return $env:CODEX_WB_CODEX
    }
    $command = Get-Command codex.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($command) {
        return $command.Source
    }
    $roots = @(
        (Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'),
        (Join-Path $env:USERPROFILE '.codex\bin')
    )
    foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root -PathType Container)) {
            continue
        }
        $found = Get-ChildItem -LiteralPath $root -Filter 'codex.exe' -File -Recurse `
            -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($found) {
            return $found.FullName
        }
    }
    throw 'Codex CLI was not found. Install Codex or set CODEX_WB_CODEX to codex.exe.'
}

$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$catalog = (Join-Path $codexHome 'workbuddy-catalog.json').Replace('\', '/')
$workspace = if ($args.Count -gt 0) { $args[0] } else { (Get-Location).Path }
$codex = Resolve-CodexCli

& $codex app $workspace `
    -c 'model_provider="workbuddy"' `
    -c 'model="cn/deepseek-v4.1-flash"' `
    -c 'model_reasoning_effort="low"' `
    -c "model_catalog_json=$catalog" `
    -c 'model_providers.workbuddy.base_url="http://127.0.0.1:8787/v1"'
exit $LASTEXITCODE
