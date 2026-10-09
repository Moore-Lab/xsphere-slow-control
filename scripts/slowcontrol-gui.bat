@echo off
REM ---------------------------------------------------------------------
REM Launch the xsphere slow control service GUI on Windows.
REM
REM Normally runs under pythonw.exe so no console window sits behind the
REM GUI. If it fails to start, run it with /console to see the traceback:
REM
REM     slowcontrol-gui.bat /console
REM
REM Any further arguments are passed through to slowcontrol.servicectl,
REM so this also works as a CLI shim:
REM
REM     slowcontrol-gui.bat /console status
REM     slowcontrol-gui.bat /console restart slowcontrol
REM ---------------------------------------------------------------------
setlocal EnableDelayedExpansion

REM Repo root is the parent of this scripts\ directory.
set "REPO=%~dp0.."
pushd "%REPO%" || (echo Could not enter "%REPO%" & pause & exit /b 1)

set "WINDOWED=1"
if /I "%~1"=="/console" (
    set "WINDOWED=0"
    shift
)

REM Collect the remaining arguments by hand: %* is not affected by shift.
set "PYARGS="
:collect
if "%~1"=="" goto resolve
set "PYARGS=!PYARGS! %1"
shift
goto collect

:resolve
REM conda base first, the same place launch_daq.bat looks: on the DAQ machine
REM that is the only real interpreter and it is not on PATH. servicectl needs
REM nothing beyond the standard library, so any Python 3 with Tk will do.
set "PYDIR="
if exist "%USERPROFILE%\anaconda3\pythonw.exe" set "PYDIR=%USERPROFILE%\anaconda3"
if not defined PYDIR if exist "%LOCALAPPDATA%\anaconda3\pythonw.exe" set "PYDIR=%LOCALAPPDATA%\anaconda3"
if defined PYDIR (
    if "%WINDOWED%"=="1" (
        start "" "!PYDIR!\pythonw.exe" -m slowcontrol.servicectl !PYARGS!
    ) else (
        "!PYDIR!\python.exe" -m slowcontrol.servicectl !PYARGS!
    )
    goto done
)

REM Then the py launcher, which finds Python even when it is not on PATH.
where py.exe >nul 2>&1
if %ERRORLEVEL%==0 (
    if "%WINDOWED%"=="1" (
        start "" pyw.exe -3 -m slowcontrol.servicectl !PYARGS!
    ) else (
        py.exe -3 -m slowcontrol.servicectl !PYARGS!
    )
    goto done
)

if "%WINDOWED%"=="1" (set "PYEXE=pythonw.exe") else (set "PYEXE=python.exe")
where %PYEXE% >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Python 3 was not found ^(looked for anaconda3, the py launcher and PATH^).
    echo Install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH", then run this again.
    echo.
    pause
    popd
    exit /b 1
)

if "%WINDOWED%"=="1" (
    start "" pythonw.exe -m slowcontrol.servicectl !PYARGS!
) else (
    python.exe -m slowcontrol.servicectl !PYARGS!
)

:done
set "RC=%ERRORLEVEL%"
popd
endlocal & exit /b %RC%
