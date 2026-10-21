@echo off
REM VC100 (medium chamber) in SIMULATION - double-click to start.
REM No cDAQ, turbos or RS-485 converter are used: safe on any PC, including the lab PC.
REM The plant runs 20x faster than real time.  Extra options are passed on, e.g.
REM   run_vc100_sim.bat --mode admin        start in Admin mode
REM   run_vc100_sim.bat --units torr        show Torr
call "%~dp0run.bat" --config config\facility_vc100.yaml --sim --time-scale 20 %*
