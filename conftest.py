"""
Conftest racine.

Sa seule présence ici (plutôt que dans tests/) garantit que pytest insère la
racine du projet dans sys.path, pour que les tests puissent faire des imports
absolus (`import database.database`, `import services.telegram`, ...) sans
installer le projet comme package.
"""
import pytest


@pytest.fixture(autouse=True)
def _reset_telegram_module_state():
    """Les services telegram/discord gardent un état global (session, dernier
    statut) — on l'isole entre tests pour éviter toute pollution croisée."""
    import services.telegram as telegram

    telegram._last_ok = None
    yield
    telegram._last_ok = None
