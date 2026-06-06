$ErrorActionPreference = "Stop"

param(
    [ValidateSet("app", "demo", "doctor", "test")]
    [string]$Mode = "demo"
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (Test-Path $venvPython) {
    $python = $venvPython
} else {
    $python = "python"
}

switch ($Mode) {
    "app" {
        & $python (Join-Path $root "app.py")
    }
    "demo" {
        & $python (Join-Path $root "browser_live_demo.py")
    }
    "doctor" {
        & $python (Join-Path $root "doctor.py")
    }
    "test" {
        & $python -m py_compile `
            (Join-Path $root "app.py") `
            (Join-Path $root "browser_live_demo.py") `
            (Join-Path $root "doctor.py")
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
        & $python (Join-Path $root "doctor.py")
    }
}

