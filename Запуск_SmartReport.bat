@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo Запуск программы SmartReport...
python app.py
if errorlevel 1 (
    echo.
    echo [ОШИБКА] Программа завершилась с ошибкой выше.
)
pause