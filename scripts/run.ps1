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

$BundledAdbDir = Join-Path $PWD "tools\platform-tools"
if (Test-Path (Join-Path $BundledAdbDir "adb.exe")) {
	$env:PATH = $BundledAdbDir + [System.IO.Path]::PathSeparator + $env:PATH
	$env:ADB = Join-Path $BundledAdbDir "adb.exe"
}

& $PythonExe main.py
