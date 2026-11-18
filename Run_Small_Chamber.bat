@echo off
REM ===========================================================================
REM  SMALL CHAMBER - start the facility control on the REAL hardware (cDAQ1).
REM  Double-click this file.  It never starts the simulator.
REM
REM  It checks Python, the NI-DAQmx driver and that LabVIEW is closed, asks
REM  once for confirmation, then opens the facility control panel.
REM  (All the work is done by python_facility_control\run_small.bat.)
REM ===========================================================================
if not exist "%~dp0python_facility_control\run_small.bat" (
    echo Cannot find python_facility_control\run_small.bat next to this file.
    echo Keep this file in the top folder of the FaciliyControl repository.
    pause
    exit /b 1
)
call "%~dp0python_facility_control\run_small.bat" %*
