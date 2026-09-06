"""
Vinted Monitor — point d'entrée.

Assemble configuration, logging, base de données, moteur de scan, services
de notification (Telegram/Discord) et interface graphique. Toute la logique
vit dans les modules dédiés (core/, database/, services/, gui/, utils/) —
ce fichier ne fait que les brancher ensemble.

Lancement :
    pip install -r requirements.txt
    python app.py
"""
import logging

from gui.app_window import App
from utils.logger import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    setup_logging(logging.INFO)
    logger.info("🛍️ Vinted Monitor — démarrage")
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
