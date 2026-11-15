# Сборка AULA F75 Knob: портативный exe (PyInstaller) + установщик (Inno Setup 6).
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

Write-Host '==> PyInstaller: сборка dist\AULA F75 Knob.exe'
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name 'AULA F75 Knob' `
    --icon 'assets\icon.ico' `
    --add-data 'ui;ui' `
    --add-data 'assets;assets' `
    --add-data 'config.json;.' `
    --collect-all webview `
    app.py

if (-not (Test-Path -LiteralPath (Join-Path $root 'dist\AULA F75 Knob.exe'))) {
    throw 'PyInstaller не создал dist\AULA F75 Knob.exe'
}

$candidates = @(
    $env:INNO_SETUP_ISCC,
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe',
    (Join-Path $root 'build\InnoSetup\ISCC.exe')
)
$iscc = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $iscc) {
    throw 'Не найден ISCC.exe (Inno Setup 6). Установите Inno Setup или задайте переменную INNO_SETUP_ISCC.'
}

Write-Host "==> Inno Setup: $iscc"
& $iscc (Join-Path $root 'packaging\installer.iss')
if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с кодом $LASTEXITCODE" }

Write-Host ''
Write-Host 'Готово:'
Write-Host '  dist\AULA F75 Knob.exe'
Get-ChildItem -LiteralPath (Join-Path $root 'packaging\Output') -Filter '*.exe' |
    ForEach-Object { Write-Host ('  ' + $_.FullName) }
