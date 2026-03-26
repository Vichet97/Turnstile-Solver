#Requires -Version 5.1
<#
.SYNOPSIS
  Create venv (optional), install deps, fetch Camoufox, verify the binary is visible to Windows, and start api_solver.py with --browser_type camoufox.

.DESCRIPTION
  Microsoft Store Python virtualizes %LocalAppData%; Playwright's Node driver cannot see Camoufox installed there.
  Use python.org / py launcher Python so camoufox.exe lands on the real filesystem.

.PARAMETER Python
  Path to python.exe (e.g. C:\Python312\python.exe). If empty, uses py launcher tags then common install dirs.

.PARAMETER PyLauncherTag
  Windows only: force the py launcher first, e.g. 3.12 -> runs 'py -3.12'. Use when 'python' / 'python3' still point at an old install (py -0p lists registered versions).

.PARAMETER VenvDir
  Relative or absolute path to the virtual environment directory (default: .venv next to this script).

.PARAMETER SkipVenv
  Use the resolved Python directly without creating/using a venv.

.PARAMETER SkipFetch
  Do not run python -m camoufox fetch.

.PARAMETER OnlySetup
  Install + fetch + verify only; do not start the API.

.PARAMETER ListPythonCandidates
  Print py -0p and python.exe paths under Local\Programs\Python, then exit (use to fill in -Python).

.PARAMETER NoAutoInstall
  Do not run winget to install Python.Python.3.12 when no usable interpreter is found (default: auto-install is on).

.PARAMETER BindAddress
  API bind address (default 0.0.0.0).

.PARAMETER BindPort
  API port (default 5000).

.PARAMETER SolverDebug
  Pass --debug to api_solver.py (named SolverDebug because PowerShell reserves -Debug).

.PARAMETER Headless
  Pass --headless to api_solver.py.

.EXAMPLE
  .\setup-and-run-camoufox-api.ps1

.EXAMPLE
  .\setup-and-run-camoufox-api.ps1 -Python 'C:\Python312\python.exe' -SolverDebug

.EXAMPLE
  .\setup-and-run-camoufox-api.ps1 -PyLauncherTag 3.12 -SolverDebug

.EXAMPLE
  .\setup-and-run-camoufox-api.ps1 -OnlySetup

.EXAMPLE
  .\setup-and-run-camoufox-api.ps1 -ListPythonCandidates

#>

[CmdletBinding()]
param(
    [string] $Python = "",
    [string] $PyLauncherTag = "",
    [string] $VenvDir = ".venv",
    [switch] $SkipVenv,
    [switch] $SkipFetch,
    [switch] $OnlySetup,
    [switch] $ListPythonCandidates,
    [switch] $NoAutoInstall,
    [string] $BindAddress = "0.0.0.0",
    [string] $BindPort = "5000",
    [switch] $SolverDebug,
    [switch] $Headless
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
if (-not $ProjectRoot) { $ProjectRoot = Get-Location }
Set-Location -LiteralPath $ProjectRoot

function Write-Info([string] $Msg) { Write-Host "[*] $Msg" -ForegroundColor Cyan }
function Write-Warn([string] $Msg) { Write-Host "[!] $Msg" -ForegroundColor Yellow }
function Write-Err([string] $Msg)  { Write-Host "[x] $Msg" -ForegroundColor Red }

function Test-ExecutablePath {
    param([string] $Exe)
    try {
        $p = (Resolve-Path -LiteralPath $Exe -ErrorAction Stop).Path
        return ($p -and (Test-Path -LiteralPath $p -PathType Leaf))
    } catch {
        return $false
    }
}

function Test-IsStorePython {
    param([string] $PythonExe)
    $full = [System.IO.Path]::GetFullPath($PythonExe)
    if ($full -match '(?i)WindowsApps\\PythonSoftwareFoundation\.') { return $true }
    if ($full -match '(?i)Microsoft\\WindowsApps\\Python') { return $true }
    return $false
}

if ($ListPythonCandidates) {
    Write-Host "=== where.exe python* (PATH) ===" -ForegroundColor Cyan
    try {
        & where.exe python 2>$null
        & where.exe python3 2>$null
    } catch { }
    Write-Host "`n=== py launcher (installs registered with py) ===" -ForegroundColor Cyan
    try {
        & py -0p 2>&1
    } catch {
        Write-Host "(py not found on PATH — typical if only Microsoft Store stub is installed)" -ForegroundColor Yellow
    }
    Write-Host "`n=== python.exe under Local\Programs\Python ===" -ForegroundColor Cyan
    $root = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (-not (Test-Path -LiteralPath $root)) {
        Write-Host "(folder does not exist: $root)" -ForegroundColor Yellow
        Write-Host "Install e.g. winget install Python.Python.3.12 then re-run with -ListPythonCandidates" -ForegroundColor Yellow
    } else {
        Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $exe = Join-Path $_.FullName "python.exe"
            if (Test-Path -LiteralPath $exe) {
                $isStore = Test-IsStorePython $exe
                Write-Host ("  {0}  (treated-as-Store-path: {1})" -f $exe, $isStore)
            }
        }
    }
    Write-Host ""
    Write-Host "Next step if nothing usable is listed: install Python (adds py + a real python.exe):" -ForegroundColor Green
    Write-Host "  winget install Python.Python.3.12" -ForegroundColor White
    Write-Host "Then close this terminal, open a new one, and run -ListPythonCandidates again." -ForegroundColor Green
    Write-Host "`nPass a printed path with: -Python '...'" -ForegroundColor Green
    exit 0
}

function Find-PythonOrgUnderPrograms {
    $bases = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        (Join-Path $env:ProgramFiles "Python312"),
        (Join-Path $env:ProgramFiles "Python311"),
        (Join-Path $env:ProgramFiles "Python310")
    )
    foreach ($base in $bases) {
        if (-not (Test-Path -LiteralPath $base)) { continue }
        if ($base -match '(?i)\\Python3\d\d$') {
            $cand = Join-Path $base "python.exe"
            if ((Test-ExecutablePath $cand) -and -not (Test-IsStorePython $cand)) {
                return [System.IO.Path]::GetFullPath($cand)
            }
            continue
        }
        foreach ($dir in Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue) {
            if ($dir.Name -notmatch '^Python\d+$') { continue }
            $cand = Join-Path $dir.FullName "python.exe"
            if ((Test-ExecutablePath $cand) -and -not (Test-IsStorePython $cand)) {
                return [System.IO.Path]::GetFullPath($cand)
            }
        }
    }
    return $null
}

function Resolve-PythonExe {
    if ($Python -ne "") {
        if (-not (Test-ExecutablePath $Python)) {
            throw "Parameter -Python not found or not a file: $Python`nRun: .\setup-and-run-camoufox-api.ps1 -ListPythonCandidates"
        }
        return (Get-Item -LiteralPath $Python).FullName
    }

    $verTags = New-Object System.Collections.Generic.List[string]
    if (-not [string]::IsNullOrWhiteSpace($PyLauncherTag)) {
        $t = $PyLauncherTag.Trim()
        if (-not $t.StartsWith('-')) { $t = "-$t" }
        [void]$verTags.Add($t)
    }
    foreach ($fallback in @("-3.12", "-3.11", "-3.10", "-3")) {
        if (-not $verTags.Contains($fallback)) { [void]$verTags.Add($fallback) }
    }
    foreach ($pyVer in $verTags) {
        try {
            $exePath = & py $pyVer -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exePath) {
                $p = ($exePath | Select-Object -Last 1).ToString().Trim()
                if ((Test-ExecutablePath $p) -and -not (Test-IsStorePython $p)) {
                    return $p
                }
            }
        } catch { }
    }

    $fromDisk = Find-PythonOrgUnderPrograms
    if ($fromDisk) { return $fromDisk }

    foreach ($cmdName in @('python3', 'python')) {
        try {
            $py = Get-Command $cmdName -ErrorAction Stop | Select-Object -ExpandProperty Source
            if ($py -and (Test-ExecutablePath $py) -and -not (Test-IsStorePython $py)) {
                return [System.IO.Path]::GetFullPath($py)
            }
        } catch { }
    }

    throw @"
Could not find a non-Store Python. Store Python breaks Camoufox + Playwright (virtualized AppData).

Discover paths on this PC:
  .\setup-and-run-camoufox-api.ps1 -ListPythonCandidates

Then either:
  1) winget install Python.Python.3.12
  2) Install from https://www.python.org/downloads/ (check 'py launcher')
  3) -Python 'full\path\python.exe' from the discovery list
"@
}

function Update-PathFromRegistry {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($null -eq $machinePath) { $machinePath = '' }
    if ($null -eq $userPath) { $userPath = '' }
    $env:Path = "$machinePath;$userPath"
}

function Invoke-WingetPython312 {
    Write-Info "Installing Python 3.12 via winget (source: winget, silent) ..."
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & winget install Python.Python.3.12 --source winget --accept-package-agreements --accept-source-agreements --silent 2>&1 | Out-Host
    } finally {
        $ErrorActionPreference = $prevEap
    }
    Update-PathFromRegistry
    Start-Sleep -Seconds 4
}

$pyExe = $null
try {
    $pyExe = Resolve-PythonExe
} catch {
    if ($NoAutoInstall) { throw }
    Write-Warn ($_.Exception.Message)
    Invoke-WingetPython312
    $pyExe = Resolve-PythonExe
}

if (Test-IsStorePython $pyExe) {
    if ($NoAutoInstall) {
        throw "Refusing to use Microsoft Store Python at:`n  $pyExe`nPass -Python to python.org python.exe or run without -NoAutoInstall."
    }
    Write-Warn "Resolved Python is Microsoft Store build (breaks Camoufox/Playwright). Installing python.org build via winget side-by-side ..."
    Invoke-WingetPython312
    $pyExe = Resolve-PythonExe
    if (Test-IsStorePython $pyExe) {
        throw "Still resolving to Store Python. Close this terminal, open a new one, and re-run; or pass -Python to `$env:LOCALAPPDATA\\Programs\\Python\\Python312\\python.exe"
    }
}

Write-Info "Using Python: $pyExe"

if (-not $SkipVenv) {
    $venvRoot = if ([System.IO.Path]::IsPathRooted($VenvDir)) { $VenvDir } else { Join-Path $ProjectRoot $VenvDir }
    $venvPy = Join-Path $venvRoot "Scripts\python.exe"
    if (-not (Test-ExecutablePath $venvPy)) {
        Write-Info "Creating venv at $venvRoot"
        & $pyExe -m venv $venvRoot
    }
    $pyExe = [System.IO.Path]::GetFullPath($venvPy)
}

Write-Info "Upgrading pip (if needed)"
& $pyExe -m pip install --upgrade pip

$req = Join-Path $ProjectRoot "requirements.txt"
if (-not (Test-Path -LiteralPath $req -PathType Leaf)) {
    throw "Missing requirements.txt in $ProjectRoot"
}
Write-Info "Installing requirements from requirements.txt"
& $pyExe -m pip install -r $req

if (-not $SkipFetch) {
    Write-Info "Running: python -m camoufox fetch"
    & $pyExe -m camoufox fetch
}

function Test-CamoufoxBinary([string] $PythonExe) {
    & $PythonExe -c @"
from camoufox.pkgman import launch_path
import os, sys
p = launch_path()
if not os.path.isfile(p):
    print('Camoufox binary missing:', p, file=sys.stderr)
    sys.exit(1)
print('Camoufox OK:', p)
"@
    return ($LASTEXITCODE -eq 0)
}

Write-Info "Verifying Camoufox binary (launch_path)"
if (-not (Test-CamoufoxBinary $pyExe)) {
    if (-not $SkipFetch) {
        Write-Warn "Re-running camoufox fetch once ..."
        & $pyExe -m camoufox fetch
        if (-not (Test-CamoufoxBinary $pyExe)) {
            throw "Camoufox binary still missing after fetch. Check disk, network, antivirus, or run: $pyExe -m camoufox fetch"
        }
    } else {
        throw "Camoufox verification failed. Re-run without -SkipFetch or execute: $pyExe -m camoufox fetch"
    }
}

if ($OnlySetup) {
    Write-Info "OnlySetup: done."
    exit 0
}

$apiArgs = @(
    "$ProjectRoot\api_solver.py",
    "--browser_type", "camoufox",
    "--host", $BindAddress,
    "--port", $BindPort
)
if ($SolverDebug) { $apiArgs += "--debug" }
if ($Headless) { $apiArgs += "--headless" }

Write-Info "Starting API: $pyExe $([string]::Join(' ', $apiArgs))"
& $pyExe @apiArgs
