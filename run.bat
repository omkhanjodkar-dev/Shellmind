@echo off
where python >nul 2>nul
if %errorlevel%==0 (python "%~dp0bootstrap.py" run %*) else (py -3 "%~dp0bootstrap.py" run %*)
