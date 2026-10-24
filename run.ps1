# H2S Mission Control (Windows): checks prerequisites, prepares .venv, starts the server.
Set-Location -Path $PSScriptRoot

function Test-Python($exe, $pre) {
    try {
        $v = & $exe @pre -c "import sys; print(int(sys.version_info >= (3, 9)))" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$v".Trim() -eq "1")
    } catch { return $false }
}

# "python" may be the Microsoft Store stub, so actually run it instead of trusting Get-Command.
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) { if (Test-Python "py" @("-3")) { $py = @("py", "-3") } }
if (-not $py -and (Get-Command python -ErrorAction SilentlyContinue)) { if (Test-Python "python" @()) { $py = @("python") } }
$ffmpeg = [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue)

if (-not $py -or -not $ffmpeg) {
    Write-Host "Не хватает / Missing:" -NoNewline
    if (-not $py) { Write-Host " python" -NoNewline }
    if (-not $ffmpeg) { Write-Host " ffmpeg" -NoNewline }
    Write-Host "`n"
    if (-not $py) { Write-Host "    winget install -e --id Python.Python.3.12" -ForegroundColor Cyan }
    if (-not $ffmpeg) { Write-Host "    winget install -e --id Gyan.FFmpeg" -ForegroundColor Cyan }
    Write-Host "`nПосле установки откройте НОВОЕ окно терминала и снова запустите run.bat"
    Write-Host "After installing, open a NEW terminal window and run run.bat again`n"
    exit 1
}

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$ready = $false
if (Test-Path $venvPy) { & $venvPy -c "import paho.mqtt" 2>$null; $ready = ($LASTEXITCODE -eq 0) }
if (-not $ready) {
    Write-Host "Первый запуск: ставлю зависимости / First run: installing dependencies..."
    & $py[0] @($py | Select-Object -Skip 1) -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit 1 }
    & $venvPy -m pip install -q --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

& $venvPy server.py
