@echo off
echo Installation des dependances...
pip install -r requirements.txt
echo.
echo Lancement de Vinted Monitor...
python app.py
pause
