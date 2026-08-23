$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot
Set-Location ..

function Test-VenvHealthy {
    param(
        [string]$VenvRoot
    )

    $PythonExe = Join-Path $VenvRoot "Scripts\python.exe"
    $CfgPath = Join-Path $VenvRoot "pyvenv.cfg"

    if (-not (Test-Path $PythonExe) -or -not (Test-Path $CfgPath)) {
        return $false
    }

    $CfgLines = Get-Content $CfgPath -ErrorAction SilentlyContinue
    $ExecutableLine = $CfgLines | Where-Object { $_ -like 'executable = *' } | Select-Object -First 1
    if ($ExecutableLine) {
        $RecordedExecutable = $ExecutableLine.Substring(13).Trim()
        if ($RecordedExecutable -and -not (Test-Path $RecordedExecutable)) {
            return $false
        }
    }

    return $true
}

function New-ProjectVenv {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
        return
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv .venv
        return
    }

    throw "Python not found. Install Python or the Python Launcher to create the virtual environment."
}

if (-not (Test-VenvHealthy ".venv") -and -not (Test-VenvHealthy "venv")) {
    foreach ($VenvRoot in @(".venv", "venv")) {
        if (Test-Path $VenvRoot) {
            Remove-Item $VenvRoot -Recurse -Force
        }
    }

    New-ProjectVenv
}

if (Test-Path ".venv\Scripts\python.exe") {
    $PythonExe = ".venv\Scripts\python.exe"
} elseif (Test-Path "venv\Scripts\python.exe") {
    $PythonExe = "venv\Scripts\python.exe"
} else {
    throw "Python not found in the virtual environment. Run bootstrap.ps1 again to create it."
}

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r requirements.txt

function Get-LatestScrcpyAssetUrl {
    $headers = @{ "User-Agent" = "Mozilla/5.0" }
    $release = Invoke-RestMethod -Headers $headers -Uri "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
    $asset = $release.assets | Where-Object { $_.name -match '^scrcpy-win64.*\.zip$' } | Select-Object -First 1
    if (-not $asset) {
        throw "Could not find a win64 scrcpy zip in the latest release."
    }
    return $asset.browser_download_url
}

function Get-LatestPlatformToolsUrl {
    return "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
}

function Install-Scrcpy {
    $toolsDir = Join-Path $PWD "tools"
    $scrcpyDir = Join-Path $toolsDir "scrcpy"
    $existingExe = Get-ChildItem -Path $scrcpyDir -Filter scrcpy.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1

    if ($existingExe) {
        Write-Host "scrcpy already exists: $($existingExe.FullName)"
        return
    }

    New-Item -ItemType Directory -Force -Path $scrcpyDir | Out-Null
    $extractDir = Join-Path $scrcpyDir ("install-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
    $zipPath = Join-Path $env:TEMP "scrcpy-win64.zip"
    $downloadUrl = Get-LatestScrcpyAssetUrl
    Write-Host "Downloading scrcpy from $downloadUrl"
    Invoke-WebRequest -Headers @{ "User-Agent" = "Mozilla/5.0" } -Uri $downloadUrl -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

    $exe = Get-ChildItem -Path $extractDir -Filter scrcpy.exe -Recurse | Select-Object -First 1
    if (-not $exe) {
        throw "Downloaded scrcpy but could not find scrcpy.exe after extraction."
    }
    Write-Host "scrcpy installed at $($exe.FullName)"
}

function Install-AdbPlatformTools {
    $adbExe = Join-Path $PWD "tools\platform-tools\adb.exe"
    if (Test-Path $adbExe) {
        Write-Host "adb already exists: $adbExe"
        return
    }

    $toolsDir = Join-Path $PWD "tools"
    $platformToolsDir = Join-Path $toolsDir "platform-tools"
    New-Item -ItemType Directory -Force -Path $platformToolsDir | Out-Null

    $extractDir = Join-Path $platformToolsDir ("install-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $extractDir | Out-Null
    $zipPath = Join-Path $env:TEMP "platform-tools.zip"
    $downloadUrl = Get-LatestPlatformToolsUrl
    Write-Host "Downloading Android platform-tools from $downloadUrl"
    Invoke-WebRequest -Headers @{ "User-Agent" = "Mozilla/5.0" } -Uri $downloadUrl -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $extractDir -Force
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue

    $sourceDir = Get-ChildItem -Path $extractDir -Directory | Select-Object -First 1
    if (-not $sourceDir) {
        throw "Downloaded platform-tools but could not find extracted folder."
    }

    Copy-Item -Path (Join-Path $sourceDir.FullName '*') -Destination $platformToolsDir -Recurse -Force
    Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "adb platform-tools installed at $platformToolsDir"
}

Install-Scrcpy
Install-AdbPlatformTools
