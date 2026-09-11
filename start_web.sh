#!/bin/bash
set -e
echo ""
echo "  ⚡  Vinted Monitor — Interface Web"
echo "  ====================================="
echo ""

pip install -r requirements.txt --quiet --no-warn-script-location
echo "Démarrage sur http://localhost:8080 ..."
python web_app.py
