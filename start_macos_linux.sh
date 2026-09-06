#!/usr/bin/env bash
set -e
echo "Installation des dépendances..."
pip3 install -r requirements.txt
echo ""
echo "Lancement de Vinted Monitor..."
python3 app.py
