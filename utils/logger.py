"""
Logging centralisé + redaction des secrets (défense en profondeur).

Aucun code du projet ne doit logger un token/webhook directement, mais on
ajoute un filet de sécurité : tout secret enregistré via `register_secret()`
est automatiquement masqué s'il apparaît dans un message de log, quelle
qu'en soit la source (exception, dépendance tierce, erreur de code futur).
"""
import logging
import sys

MIN_SECRET_LEN = 6


class RedactSecretsFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self._secrets: list[str] = []

    def register(self, value: str) -> None:
        if value and len(value) >= MIN_SECRET_LEN and value not in self._secrets:
            self._secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


_redact_filter = RedactSecretsFilter()


def register_secret(value: str) -> None:
    """À appeler dès qu'un secret (bot token, webhook URL) est lu depuis .env."""
    _redact_filter.register(value)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_redact_filter)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[handler],
        force=True,
    )
