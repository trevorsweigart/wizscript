param([string]$Python = "python")

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    $buildPython = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $buildPython)) {
        & $Python -m venv .venv-build
        if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment." }
    }
    # Wizwalker can change its Git commit without changing its package version.
    # Refresh only that dependency explicitly before resolving the requirements.
    $wizwalkerRequirement = Get-Content requirements.txt | Where-Object { $_ -match '^wizwalker\s*@' }
    & $buildPython -m pip install --disable-pip-version-check --force-reinstall --no-deps $wizwalkerRequirement
    if ($LASTEXITCODE -ne 0) { throw "Could not update Wizwalker from GitHub." }

    & $buildPython -m pip install --disable-pip-version-check -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Could not install build dependencies." }

    & $buildPython -m PyInstaller --clean --noconfirm WizScript.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    Write-Host "Built: $(Join-Path $PSScriptRoot 'dist\WizScript.exe')"
} finally {
    Pop-Location
}
