$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$testRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'tmp'))
$testBase = [System.IO.Path]::GetFullPath((Join-Path $testRoot ('pytest-' + [guid]::NewGuid().ToString('N'))))
if (-not $testBase.StartsWith($testRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Test directory escaped workspace'
}
if (Test-Path -LiteralPath $testBase) { throw 'Test directory must be new' }
$null = [System.IO.Directory]::CreateDirectory($testRoot)
$testPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $testPython -PathType Leaf)) { $testPython = 'python' }
& $testPython -m pytest tests --basetemp $testBase -q @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
