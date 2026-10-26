@echo off
REM ===========================================================================
REM  VC100 (medium chamber) - REAL HARDWARE.  Double-click to start.
REM
REM  Always uses config\facility_vc100.yaml and the real NI cDAQ (cDAQ3).
REM  It never falls back to the simulator: if the NI-DAQmx driver or the
REM  nidaqmx package is missing it stops with a message instead.
REM  For the simulator use run_vc100_sim.bat.
REM
REM  Extra options are passed on, e.g.   run_vc100.bat --mode admin
REM ===========================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title VC100 Facility Control - REAL HARDWARE
if "%FACILITY_VENV%"=="" set "FACILITY_VENV=%LOCALAPPDATA%\FacilityControl\venv"
set "PY=%FACILITY_VENV%\Scripts\python.exe"

REM ---- 1. Python environment (same place as run.bat) -----------------------
if not exist "%PY%" (
    echo Creating Python environment in "%FACILITY_VENV%" ...
    py -3 -m venv "%FACILITY_VENV%" 2>nul || python -m venv "%FACILITY_VENV%"
    if not exist "%PY%" (
        echo.
        echo Could not create the Python environment.  Is Python 3.10 or newer installed and on PATH?
        goto :fail
    )
)

"%PY%" -c "import PySide6.QtWidgets, pyqtgraph, numpy, yaml, serial" >nul 2>&1
if errorlevel 1 (
    echo Installing / repairing dependencies ...
    "%PY%" -m pip install --upgrade pip >nul 2>&1
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :installfailed
)

REM ---- 2. NI-DAQmx Python package (not in requirements.txt) ----------------
"%PY%" -c "import nidaqmx" >nul 2>&1
if errorlevel 1 (
    echo Installing the nidaqmx package ...
    "%PY%" -m pip install nidaqmx
    if errorlevel 1 goto :installfailed
)

REM ---- 3. NI-DAQmx driver present? ------------------------------------------
"%PY%" -c "from nidaqmx.system import System; print('NI-DAQmx driver', System.local().driver_version)" 2>nul
if errorlevel 1 (
    echo.
    echo  The NI-DAQmx DRIVER was not found on this PC.
    echo  Install NI-DAQmx from ni.com and check in NI MAX that cDAQ3 is visible.
    goto :fail
)

REM ---- 4. LabVIEW must not hold the cDAQ -------------------------------------
tasklist /FI "IMAGENAME eq LabVIEW.exe" 2>nul | find /I "LabVIEW.exe" >nul
if not errorlevel 1 (
    echo.
    echo  LabVIEW is running.  Close LabVIEW first - it cannot share the cDAQ with this program.
    goto :fail
)

REM ---- 5. Operator confirmation ----------------------------------------------
echo.
echo  ======================================================================
echo   VC100 - REAL HARDWARE
echo   At start-up the program puts the facility in its SAFE STATE:
echo   all valves closed, turbos stopped, primary pump and chiller OFF.
echo   Only continue if the facility is stopped / safe to switch off.
echo  ======================================================================
choice /C YN /N /M " Start VC100 Facility Control now? [Y/N] "
if errorlevel 2 (
    echo Cancelled.
    endlocal
    exit /b 0
)

REM ---- 6. Run -----------------------------------------------------------------
"%PY%" run_facility.py --config config\facility_vc100.yaml --daq %*
if errorlevel 1 goto :fail
endlocal
exit /b 0

:installfailed
echo.
echo  Dependency installation failed (network/proxy, or Windows long paths).
echo  See run.bat / README.md for the fixes.
:fail
echo.
pause
endlocal
exit /b 1
