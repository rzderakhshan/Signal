$ErrorActionPreference = "Stop"

# One-click updater for Reza's Hyperliquid Whale Tracker.
$BaseDir = "D:\Program"
$RepoDir = "D:\Program\mnt\data\Signal_whale_tracker"
$RepoUrl = "https://github.com/rzderakhshan/Signal.git"
$Branch = "production-scanner-v1"
$Workflow = "Hyperliquid Whale Tracker"

Write-Host "=== Hyperliquid Whale Tracker: deploy + run ===" -ForegroundColor Cyan

# Find the newest downloaded package in D:\Program.
$Zip = Get-ChildItem -Path $BaseDir -Filter "Signal_whale_tracker_v*.zip" -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $Zip) {
    throw "No Signal_whale_tracker_v*.zip found in $BaseDir"
}
Write-Host "Package: $($Zip.FullName)" -ForegroundColor Green

# Make sure GitHub CLI can be found even in an older PowerShell session.
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    $GhDir = "C:\Program Files\GitHub CLI"
    if (Test-Path "$GhDir\gh.exe") {
        $env:Path += ";$GhDir"
    } else {
        Write-Host "Installing GitHub CLI..." -ForegroundColor Yellow
        winget install --id GitHub.cli --accept-source-agreements --accept-package-agreements
        $env:Path += ";$GhDir"
    }
}

# Clone the repository if it is missing. If a non-git folder occupies the path, back it up first.
if (-not (Test-Path "$RepoDir\.git")) {
    if (Test-Path $RepoDir) {
        $Backup = "$RepoDir-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Write-Host "Existing non-git folder moved to $Backup" -ForegroundColor Yellow
        Move-Item $RepoDir $Backup
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $RepoDir) | Out-Null
    git clone -b $Branch $RepoUrl $RepoDir
}

# Extract to a temporary folder, then copy package contents over the repository.
$TempDir = Join-Path $env:TEMP "Signal_whale_tracker_update"
if (Test-Path $TempDir) { Remove-Item $TempDir -Recurse -Force }
New-Item -ItemType Directory -Path $TempDir | Out-Null
Expand-Archive -Path $Zip.FullName -DestinationPath $TempDir -Force
Get-ChildItem -Path $TempDir -Force | ForEach-Object {
    Copy-Item $_.FullName -Destination $RepoDir -Recurse -Force
}
Remove-Item $TempDir -Recurse -Force

Set-Location $RepoDir

# Verify v4 landed before changing GitHub.
$VersionCheck = Select-String -Path ".\whale_tracker.py" -Pattern "alerts_flushed=true|TELEGRAM_BUNDLE_MAX_CHARS|TOP_WHALES" -Quiet
if (-not $VersionCheck) {
    throw "v4 verification failed. whale_tracker.py does not contain the expected v4 markers."
}

# Commit only when there are changes.
git add -A
$HasChanges = -not (git diff --cached --quiet; $LASTEXITCODE -eq 0)
if ($HasChanges) {
    git commit -m "Deploy whale tracker v4 with Telegram batching and rate-limit protection"
    git push origin $Branch
} else {
    Write-Host "Repository already contains this version; no commit needed." -ForegroundColor DarkGray
    git push origin $Branch
}

# Authenticate GitHub CLI if needed.
gh auth status 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "GitHub CLI login is required..." -ForegroundColor Yellow
    gh auth login
}

# Trigger a real run and force a fresh Top-10 ranking after deployment.
Write-Host "Starting GitHub workflow with forced Top-10 refresh..." -ForegroundColor Cyan
gh workflow run $Workflow --ref $Branch -f refresh_ranking=true
Start-Sleep -Seconds 4

$Run = gh run list --workflow $Workflow --branch $Branch --event workflow_dispatch --limit 1 --json databaseId,status,url,createdAt | ConvertFrom-Json
if (-not $Run) { throw "Could not find the newly created workflow run." }
$RunId = $Run[0].databaseId
Write-Host "Run ID: $RunId" -ForegroundColor Green
Write-Host "Waiting for completion..." -ForegroundColor Cyan
gh run watch $RunId --exit-status

Write-Host "`n=== Important log lines ===" -ForegroundColor Cyan
gh run view $RunId --log | Select-String "Done. whales|ranking|consensus|MAIN WHALE|Telegram rate|alerts_flushed|ERROR|WARNING"

Write-Host "`nGitHub Actions page will open in your browser." -ForegroundColor Green
Start-Process "https://github.com/rzderakhshan/Signal/actions"
Write-Host "Finished." -ForegroundColor Green
