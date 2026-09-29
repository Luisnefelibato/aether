$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
if (Test-Path .\.venv\Scripts\Activate.ps1) {
  . .\.venv\Scripts\Activate.ps1
}
py -3 -m aether_core.main @args
