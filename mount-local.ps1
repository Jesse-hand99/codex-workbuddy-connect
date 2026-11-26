[CmdletBinding()]
param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" })
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourcePath = Join-Path $repoRoot "plugins\codex-workbuddy-connect"
$sourceManifest = Join-Path $sourcePath ".codex-plugin\plugin.json"
$marketplaceName = "workbuddy"

if (-not (Test-Path -LiteralPath $sourceManifest -PathType Leaf)) {
    throw "Plugin manifest not found: $sourceManifest"
}

$manifest = Get-Content -LiteralPath $sourceManifest -Raw -Encoding UTF8 | ConvertFrom-Json
$pluginName = [string]$manifest.name
$pluginVersion = [string]$manifest.version
if (-not $pluginName -or -not $pluginVersion) {
    throw "Plugin name or version is missing from $sourceManifest"
}

$codex = Get-Command codex.exe, codex.cmd, codex -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $codex) {
    throw "Codex CLI was not found on PATH."
}

$marketplaceRoot = [System.IO.Path]::GetFullPath($CodexHome)
$marketplaceList = & $codex.Source plugin marketplace list 2>&1 | Out-String
if ($marketplaceList -notmatch "(?m)^\s*$([regex]::Escape($marketplaceName))\s+") {
    & $codex.Source plugin marketplace add $repoRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to register local marketplace: $repoRoot"
    }
}

$cacheRoot = Join-Path $marketplaceRoot "plugins\cache\$marketplaceName\$pluginName"
$cachePath = Join-Path $cacheRoot $pluginVersion
$backupRoot = Join-Path $marketplaceRoot "plugins\cache\$marketplaceName\.link-backups"

if (-not (Test-Path -LiteralPath $cachePath -PathType Container)) {
    & $codex.Source plugin add "$pluginName@$marketplaceName"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install $pluginName@$marketplaceName"
    }
}

if (-not (Test-Path -LiteralPath $cachePath -PathType Container)) {
    throw "Codex did not create the expected plugin cache: $cachePath"
}

function Resolve-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
}

function Test-DirectoryLink {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Target
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return $false
    }

    $item = Get-Item -LiteralPath $Path -Force
    if ($item.LinkType -notin @("Junction", "SymbolicLink")) {
        return $false
    }

    $linkedTarget = @($item.Target) | Select-Object -First 1
    if (-not $linkedTarget) {
        return $false
    }

    return (Resolve-NormalizedPath $linkedTarget) -eq (Resolve-NormalizedPath $Target)
}

$sourceDirectories = Get-ChildItem -LiteralPath $sourcePath -Directory -Force |
    Where-Object { $_.Name -notin @(".codex-plugin", ".venv", "dev") }

$needsRebuild = $false
$metadataPath = Join-Path $cachePath ".codex-plugin"
$cachedManifest = Join-Path $metadataPath "plugin.json"

if (Test-Path -LiteralPath $cachePath) {
    $cacheItem = Get-Item -LiteralPath $cachePath -Force
    if ($cacheItem.LinkType) {
        $needsRebuild = $true
    }
}

if (Test-Path -LiteralPath $metadataPath) {
    $metadataItem = Get-Item -LiteralPath $metadataPath -Force
    if ($metadataItem.LinkType -or -not $metadataItem.PSIsContainer) {
        $needsRebuild = $true
    }
}
else {
    $needsRebuild = $true
}

if (Test-Path -LiteralPath $cachedManifest) {
    $manifestItem = Get-Item -LiteralPath $cachedManifest -Force
    if ($manifestItem.LinkType -or -not (Test-Path -LiteralPath $cachedManifest -PathType Leaf)) {
        $needsRebuild = $true
    }
}
else {
    $needsRebuild = $true
}

foreach ($directory in $sourceDirectories) {
    $cacheDirectory = Join-Path $cachePath $directory.Name
    if (-not (Test-DirectoryLink -Path $cacheDirectory -Target $directory.FullName)) {
        $needsRebuild = $true
        break
    }
}

if ($needsRebuild) {
    $resolvedCacheRoot = Resolve-NormalizedPath $cacheRoot
    $expectedSuffix = "plugins\cache\$marketplaceName\$pluginName"
    if (-not $resolvedCacheRoot.EndsWith($expectedSuffix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to rebuild an unexpected cache path: $resolvedCacheRoot"
    }

    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
    $backupName = "{0}.copy-backup-{1}" -f $pluginVersion, (Get-Date -Format "yyyyMMdd-HHmmss")
    $backupPath = Join-Path $backupRoot $backupName
    Move-Item -LiteralPath $cachePath -Destination $backupPath
    Write-Host "Moved the copied plugin cache to: $backupPath"

    New-Item -ItemType Directory -Path $cachePath -Force | Out-Null
}

New-Item -ItemType Directory -Path $metadataPath -Force | Out-Null
Copy-Item -LiteralPath $sourceManifest -Destination $cachedManifest -Force

foreach ($directory in $sourceDirectories) {
    $cacheDirectory = Join-Path $cachePath $directory.Name
    if (-not (Test-Path -LiteralPath $cacheDirectory)) {
        New-Item -ItemType Junction -Path $cacheDirectory -Target $directory.FullName | Out-Null
    }
}

$sourceFiles = Get-ChildItem -LiteralPath $sourcePath -File -Force |
    Where-Object { $_.Name -notin @(".gitignore") }
foreach ($file in $sourceFiles) {
    $cacheFile = Join-Path $cachePath $file.Name
    if (-not (Test-Path -LiteralPath $cacheFile)) {
        New-Item -ItemType SymbolicLink -Path $cacheFile -Target $file.FullName | Out-Null
    }
}

$installed = & $codex.Source plugin list -m $marketplaceName --json | Out-String | ConvertFrom-Json
$match = @($installed.installed) | Where-Object { $_.pluginId -eq "$pluginName@$marketplaceName" }
if (-not $match -or -not $match.installed -or -not $match.enabled) {
    throw "Codex did not report $pluginName@$marketplaceName as installed and enabled."
}

Write-Host ""
Write-Host "Local plugin mount is ready."
Write-Host "Source : $sourcePath"
Write-Host "Cache  : $cachePath"
Write-Host "State  : installed=$($match.installed), enabled=$($match.enabled), version=$($match.version)"
Write-Host ""
Write-Host "Run once after this mount:"
Write-Host "  `$env:WORKBUDDY_AI_PYTHON='$sourcePath\.venv\Scripts\python.exe'"
Write-Host "  & `$env:WORKBUDDY_AI_PYTHON '$sourcePath\bin\setup.py' install"
