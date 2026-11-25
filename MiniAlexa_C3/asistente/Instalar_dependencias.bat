@echo off
cd /d "%~dp0"
py -3.11 --version >nul 2>&1
if errorlevel 1 (
 echo Instala Python 3.11 desde python.org incluyendo el lanzador py.
 pause
 exit /b 1
)
if not exist ".venv\Scripts\python.exe" py -3.11 -m venv .venv
if errorlevel 1 goto fallo
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fallo
echo Instalacion completada. Escribe tu clave en config.json y abre Iniciar_asistente.bat.
pause
exit /b 0
:fallo
echo No se completo la instalacion. Revisa el mensaje anterior y la conexion a Internet.
pause
exit /b 1
