$ErrorActionPreference = 'Stop'

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup = [Environment]::GetFolderPath('Startup')
$lnkPath = Join-Path $startup 'AULA F75 Knob.lnk'

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = (Get-Command wscript.exe).Source
$lnk.Arguments = '"' + (Join-Path $dir 'start_hidden.vbs') + '"'
$lnk.WorkingDirectory = $dir
$lnk.Description = 'AULA F75 Knob'
$lnk.Save()

Write-Host "Startup shortcut created: $lnkPath"
