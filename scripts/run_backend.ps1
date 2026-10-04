# Run the ISMS processing node (API + web HMI) from a checkout on Windows.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..\backend')

$python = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
$port = if ($env:PI_PORT) { $env:PI_PORT } else { '8000' }

if (-not (Test-Path 'anomaly_model.pkl')) { & $python train_anomaly_model.py }
& $python -m uvicorn server:app --host 0.0.0.0 --port $port
