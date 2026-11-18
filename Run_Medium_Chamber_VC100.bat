@echo off
REM ===========================================================================
REM  MEDIUM CHAMBER (VC100) - start the facility control on the REAL hardware
REM  (cDAQ3 + the RS-485 turbos).  Double-click this file.  It never starts
REM  the simulator.
REM
REM  It checks Python, the NI-DAQmx driver and that LabVIEW is closed, asks
REM  once for confirmation, then opens the facility control panel.
REM  (All the work is done by python_facility_control\run_vc100.bat.)
REM ===========================================================================
if not exist "%~dp0python_facility_control\run_vc100.bat" (
    echo Cannot find python_facility_control\run_vc100.bat next to this file.
    echo Keep this file in the top folder of the FaciliyControl repository.
    pause
    exit /b 1
)
call "%~dp0python_facility_control\run_vc100.bat" %*
