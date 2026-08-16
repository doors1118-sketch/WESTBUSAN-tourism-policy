[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Resolve-Path -LiteralPath (Join-Path $repository '.venv\Scripts\python.exe')).Path
$logDirectory = Join-Path $repository 'logs'

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$seoulNow = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
    [DateTimeOffset]::UtcNow,
    'Korea Standard Time'
)
$asOf = $seoulNow.ToString('yyyy-MM-dd')

Push-Location -LiteralPath $repository
try {
    & $pythonPath -m westbusan.cli daily --as-of $asOf --root $repository
    $pipelineExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $pipelineExitCode
