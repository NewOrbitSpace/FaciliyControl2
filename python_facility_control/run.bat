@echo off
REM Facility Control launcher (Windows).
REM   run.bat                          start the GUI (creates/repairs the Python environment first)
REM   run.bat --sim --time-scale 20    fast simulation demo
REM   run.bat --daq                    require the real NI cDAQ (see README for installing nidaqmx)
REM   run.bat --units mbar --mode admin
REM
REM The virtual environment is created in %LOCALAPPDATA%\FacilityControl\venv (short path, outside
REM Documents/OneDrive) because PySide6 contains files whose paths exceed Windows' 260-character
REM limit when the project sits in a deep folder.  Override with:  set FACILITY_VENV=D:\some\path
setlocal EnableExtensions
cd /d "%~dp0"
if "%FACILITY_VENV%"=="" set "FACILITY_VENV=%LOCALAPPDATA%\FacilityControl\venv"
set "PY=%FACILITY_VENV%\Scripts\python.exe"

if not exist "%PY%" (
    echo Creating Python environment in "%FACILITY_VENV%" ...
    py -3 -m venv "%FACILITY_VENV%" 2>nul || python -m venv "%FACILITY_VENV%"
    if not exist "%PY%" (
        echo.
        echo Could not create the Python environment.  Is Python 3.10 or newer installed and on PATH?
        pause
        exit /b 1
    )
)

"%PY%" -c "import PySide6.QtWidgets, pyqtgraph, numpy, yaml" >nul 2>&1
if errorlevel 1 (
    echo Installing / repairing dependencies ...
    "%PY%" -m pip install --upgrade pip >nul 2>&1
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 goto :installfailed
    "%PY%" -c "import PySide6.QtWidgets, pyqtgraph, numpy, yaml" >nul 2>&1
    if errorlevel 1 goto :installfailed
)

"%PY%" run_facility.py %*
if errorlevel 1 pause
endlocal
exit /b 0

:installfailed
echo.
echo ===============================================================================
echo  Dependency installation failed.  Common causes:
echo   - network blocked / proxy: retry, or install from a machine with internet access
echo   - "No such file or directory" / long-path errors: enable Windows long paths
echo     (Settings ^> System ^> For developers ^> "Enable Win32 long paths", or see the README),
echo     or set FACILITY_VENV to a short path such as C:\fc-venv and run again
echo   - a half-installed environment: delete "%FACILITY_VENV%" and run again
echo ===============================================================================
pause
exit /b 1
