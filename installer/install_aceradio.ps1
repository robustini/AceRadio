$ErrorActionPreference = 'Stop'

$repoArchiveUrl = 'https://github.com/robustini/AceRadio/archive/refs/heads/main.zip'
$workingRoot = (Get-Location).Path
$aceStepDir = Join-Path $workingRoot 'acestep'

if (-not (Test-Path $aceStepDir -PathType Container)) {
    throw "Current directory does not look like an ACE-Step root. Missing folder: $aceStepDir"
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("aceradio_install_" + [System.Guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $tempRoot 'aceradio_main.zip'
$extractDir = Join-Path $tempRoot 'extract'

New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
New-Item -ItemType Directory -Force -Path $extractDir | Out-Null

try {
    Write-Host "Downloading AceRadio repository archive..."
    Invoke-WebRequest -Uri $repoArchiveUrl -OutFile $zipPath

    Write-Host "Extracting archive..."
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force

    $repoRoot = Join-Path $extractDir 'AceRadio-main'
    if (-not (Test-Path $repoRoot -PathType Container)) {
        $firstDir = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1
        if ($null -eq $firstDir) {
            throw "Unable to locate extracted repository root."
        }
        $repoRoot = $firstDir.FullName
    }

    $sourceUi = Join-Path $repoRoot 'acestep/ui/aceradio'
    $sourceBat = Join-Path $repoRoot 'start_aceradio_ui.bat'
    $sourceSh = Join-Path $repoRoot 'start_aceradio_ui.sh'

    if (-not (Test-Path $sourceUi -PathType Container)) {
        throw "Missing source folder in archive: acestep/ui/aceradio"
    }
    if (-not (Test-Path $sourceBat -PathType Leaf)) {
        throw "Missing source file in archive: start_aceradio_ui.bat"
    }
    if (-not (Test-Path $sourceSh -PathType Leaf)) {
        throw "Missing source file in archive: start_aceradio_ui.sh"
    }

    $targetUiParent = Join-Path $workingRoot 'acestep/ui'
    $targetUi = Join-Path $targetUiParent 'aceradio'
    $targetBat = Join-Path $workingRoot 'start_aceradio_ui.bat'
    $targetSh = Join-Path $workingRoot 'start_aceradio_ui.sh'

    New-Item -ItemType Directory -Force -Path $targetUiParent | Out-Null

    if (Test-Path $targetUi) {
        Write-Host "Removing previous acestep/ui/aceradio..."
        Remove-Item -Recurse -Force $targetUi
    }

    Write-Host "Installing AceRadio files..."
    Copy-Item -Path $sourceUi -Destination $targetUiParent -Recurse -Force
    Copy-Item -Path $sourceBat -Destination $targetBat -Force
    Copy-Item -Path $sourceSh -Destination $targetSh -Force

    Write-Host ""
    Write-Host "AceRadio installation completed."
    Write-Host "To run AceRadio, launch start_aceradio_ui.bat or start_aceradio_ui.sh from the ACE-Step root."
}
finally {
    if (Test-Path $tempRoot) {
        Remove-Item -Recurse -Force $tempRoot
    }
}
