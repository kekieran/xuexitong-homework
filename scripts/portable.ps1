param([ValidateSet('install', 'start', 'check', 'login', 'chrome')][string]$Mode = 'start')
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectDirectory

# The cmd entry points contain ASCII only, so Chinese/space-containing paths
# survive regardless of the terminal code page.
$candidates = @()
$venvConfiguration = Join-Path $projectDirectory '.venv\pyvenv.cfg'
if (Test-Path -LiteralPath $venvConfiguration) {
    $baseLine = Get-Content -LiteralPath $venvConfiguration -Encoding UTF8 | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
    if ($baseLine) {
        $baseDirectory = ($baseLine -split '=', 2)[1].Trim()
        $basePython = Join-Path $baseDirectory 'python.exe'
        if (Test-Path -LiteralPath $basePython) { $candidates += ,@($basePython) }
    }
}
foreach ($commandName in @('py', 'python', 'python3')) {
    $foundCommand = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($foundCommand) {
        if ($commandName -eq 'py') { $candidates += ,@($foundCommand.Source, '-3') }
        else { $candidates += ,@($foundCommand.Source) }
    }
}
$pythonCommand = $null
foreach ($candidate in $candidates) {
    $arguments = @($candidate | Select-Object -Skip 1)
    try {
        & $candidate[0] @arguments -I -c 'import sys,struct; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) and struct.calcsize(chr(80)) == 8 else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $pythonCommand = $candidate; break }
    } catch { continue }
}
if (-not $pythonCommand) {
    Write-Host '64-bit Python 3.10-3.14 is required. Install Python from the official page, then run this script again.'
    Write-Host 'Choose "Add python.exe to PATH" in the installer.'
    Start-Process 'https://www.python.org/downloads/windows/'
    exit 1
}
$pythonArguments = @($pythonCommand | Select-Object -Skip 1)
& $pythonCommand[0] @pythonArguments -I (Join-Path $PSScriptRoot 'homework_bootstrap.py') $Mode
exit $LASTEXITCODE
