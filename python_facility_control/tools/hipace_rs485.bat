@echo off
REM HiPace RS-485 test panel (Turbos 2 and 3, VC100).  Independent of the facility program.
REM   tools\hipace_rs485.bat              open the panel, pick the COM port(s) in the window
REM   tools\hipace_rs485.bat --sim        both turbos on the simulator
REM Uses the facility's Python environment (run run.bat once first) and adds pyserial to it.
setlocal EnableExtensions
cd /d "%~dp0"
if "%FACILITY_VENV%"=="" set "FACILITY_VENV=%LOCALAPPDATA%\FacilityControl\venv"
set "PY=%FACILITY_VENV%\Scripts\python.exe"
if not exist "%PY%" (
    echo The facility Python environment was not found in "%FACILITY_VENV%".
    echo Run ..\run.bat once to create it, then start this again.
    pause
    exit /b 1
)
"%PY%" -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo Installing pyserial ...
    "%PY%" -m pip install "pyserial>=3.5"
)
"%PY%" hipace_rs485_gui.py %*
if errorlevel 1 pause
endlocal
