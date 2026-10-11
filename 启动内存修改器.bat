@echo off
setlocal
set "PY_C98=C:\Users\lenovo\AppData\Local\Doubao\User Data\sandbox_runtime\bases\c98c5042338ed152c6f10ecd8591889f\python\python.exe"
set "PY_D0A=C:\Users\lenovo\AppData\Local\Doubao\User Data\sandbox_runtime\bases\d0ace100fdff5b2d247847aca1e3d83b\python\python.exe"
set "SCRIPT=%~dp0memory_trainer.py"
if exist "%PY_C98%" (
    "%PY_C98%" "%SCRIPT%"
    goto :end
)
if exist "%PY_D0A%" (
    "%PY_D0A%" "%SCRIPT%"
    goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
    python "%SCRIPT%"
    goto :end
)
echo Python not found. Install Python 3.10+ with tkinter.
pause
:end
endlocal
