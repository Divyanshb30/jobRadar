# JobRadar daily runner (Windows Task Scheduler entry point).
# Runs one full scan using the project's virtualenv and appends output to a log.
# Schedule it with the schtasks command in the README ("Daily run" section).

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$python = Join-Path $PSScriptRoot "jobradar\Scripts\python.exe"
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"

& $python main.py *>> (Join-Path $logDir "run_$stamp.log")
