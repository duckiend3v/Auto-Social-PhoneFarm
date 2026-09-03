$ErrorActionPreference = "Stop"

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

if (-not (Test-VenvHealthy ".venv") -and -not (Test-VenvHealthy "venv")) {
	& "$PSScriptRoot\bootstrap.ps1"
}

if (Test-Path ".venv\Scripts\python.exe") {
	$PythonExe = ".venv\Scripts\python.exe"
} elseif (Test-Path "venv\Scripts\python.exe") {
	$PythonExe = "venv\Scripts\python.exe"
} else {
	throw "Không tìm thấy Python trong môi trường ảo. Hãy chạy bootstrap.ps1 trước."
}

# Tu dong khoi phuc ADB chuan tu TLCHelper neu co
if (Test-Path "C:\TLCHelper\sdk\platform-tools\adb.exe") {
	if (-not (Test-Path "tools")) { New-Item -ItemType Directory -Path "tools" | Out-Null }
	if (-not (Test-Path "tools\platform-tools")) { New-Item -ItemType Directory -Path "tools\platform-tools" | Out-Null }
	Copy-Item -Path "C:\TLCHelper\sdk\platform-tools\*" -Destination "tools" -Recurse -Force -ErrorAction SilentlyContinue
	Copy-Item -Path "C:\TLCHelper\sdk\platform-tools\*" -Destination "tools\platform-tools" -Recurse -Force -ErrorAction SilentlyContinue
}

$HasAdb = (Test-Path "tools\adb.exe") -or (Test-Path "tools\platform-tools\adb.exe") -or (Test-Path "C:\TLCHelper\sdk\platform-tools\adb.exe")
if (-not $HasAdb) {
	Write-Host "Chưa có ADB trên máy này, đang tự động tải và cài đặt platform-tools..."
	& "$PSScriptRoot\bootstrap.ps1"
}

$BundledAdbDir = Join-Path $PWD "tools\platform-tools"
if (Test-Path (Join-Path $PWD "tools\adb.exe")) {
	$env:PATH = (Join-Path $PWD "tools") + [System.IO.Path]::PathSeparator + $env:PATH
	$env:ADB = Join-Path $PWD "tools\adb.exe"
} elseif (Test-Path (Join-Path $BundledAdbDir "adb.exe")) {
	$env:PATH = $BundledAdbDir + [System.IO.Path]::PathSeparator + $env:PATH
	$env:ADB = Join-Path $BundledAdbDir "adb.exe"
}

& $PythonExe main.py
