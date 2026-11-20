# Starts the GitHub triage dashboard if nothing is listening on its port.
param([int]$Port = 8765)
$dir = $PSScriptRoot
if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    exit 0
}
Start-Process -FilePath python -ArgumentList 'triage.py', 'serve', '--port', $Port, '--no-browser' -WorkingDirectory $dir `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $dir 'dashboard.log') `
    -RedirectStandardError (Join-Path $dir 'dashboard.err.log')
@{systemMessage = "GitHub triage dashboard starting on http://127.0.0.1:$Port"} | ConvertTo-Json -Compress
