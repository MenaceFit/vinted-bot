@echo off
title Vinted Monitor Web
chcp 65001 >nul 2>&1
cls
echo.
echo   ⚡  Vinted Monitor — Interface Web
echo   =====================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERREUR] Python non installe.
    pause & exit /b 1
)

echo Installation des dependances...
pip install -r requirements.txt --quiet --no-warn-script-location
if %errorlevel% neq 0 (
    echo [ERREUR] Impossible d'installer les dependances.
    pause & exit /b 1
)

echo.
echo Demarrage du serveur...
echo Ouvrez http://localhost:8080 dans votre navigateur
echo.
python web_app.py

if %errorlevel% neq 0 (
    echo.
    echo [ERREUR] Le serveur s'est arrete (code %errorlevel%).
    pause
)
