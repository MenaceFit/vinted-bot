"""
Configuration — réglages non sensibles dans config.json, secrets dans .env.

Séparation volontaire (voir README, section Sécurité) :
  - config.json : intervalle, marchés, mots-clés, filtres prix, toggles UI
  - .env        : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK_URL

Une config v3 existante (webhook_url en clair dans config.json) est migrée
automatiquement vers .env au premier chargement, sans jamais logger la valeur.
"""
import json
import logging
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"

MARKET_DEFAULTS = {
    "fr": {"label": "🇫🇷 France", "base_url": "https://www.vinted.fr"},
    "pl": {"label": "🇵🇱 Pologne", "base_url": "https://www.vinted.pl"},
    "uk": {"label": "🇬🇧 Angleterre", "base_url": "https://www.vinted.co.uk"},
}

SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL")


def load_env() -> None:
    """Charge .env dans os.environ (no-op silencieux si le fichier n'existe pas)."""
    load_dotenv(ENV_PATH, override=False)


def get_secret(name: str, default: str = "") -> str:
    import os
    from utils.logger import register_secret

    value = os.environ.get(name, default) or default
    register_secret(value)
    return value


def set_secret(name: str, value: str) -> None:
    """Écrit/actualise une clé dans .env sans toucher aux autres, puis recharge os.environ."""
    import os

    from utils.logger import register_secret

    if name not in SECRET_KEYS:
        raise ValueError(f"Unknown secret key: {name}")

    existing = dict(dotenv_values(ENV_PATH)) if ENV_PATH.exists() else {}
    existing[name] = value

    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[name] = value
    register_secret(value)


def mask(value: str, keep: int = 4) -> str:
    """Masque une valeur sensible pour l'affichage/logs (garde les `keep` derniers caractères)."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def _default_config() -> dict:
    return {
        "interval_seconds": 8,
        "warmup_first_run": True,
        "min_price": None,
        "max_price": None,
        # Vides par défaut à dessein : les catalog_ids/brand_ids sont des IDs
        # numériques internes Vinted qui ne correspondent pas forcément à la
        # même catégorie/marque sur tous les marchés (FR/UK/PL). Un filtre
        # numérique invalide pour un marché renvoie silencieusement 0 résultat
        # même avec une session valide — le mot-clé (search_text) suffit et
        # fonctionne de façon identique sur tous les marchés.
        "catalog_ids": [],
        "brand_ids": [],
        "keywords_filter": [],
        "custom_url": "",
        "markets": {
            key: {"enabled": key == "fr", **defaults}
            for key, defaults in MARKET_DEFAULTS.items()
        },
        "telegram": {
            "enabled": False,
            "send_new": True,
            "send_images": True,
            "sound_enabled": False,
        },
        "discord": {
            "enabled": False,
        },
    }


def load_config() -> dict:
    load_env()

    cfg = _default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except Exception:
        loaded = {}

    _deep_merge(cfg, loaded)

    for key, defaults in MARKET_DEFAULTS.items():
        m = cfg["markets"].setdefault(key, {"enabled": False})
        m.setdefault("label", defaults["label"])
        m.setdefault("base_url", defaults["base_url"])

    _migrate_legacy_secrets(cfg, loaded)

    return cfg


def _deep_merge(base: dict, overrides: dict) -> None:
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _migrate_legacy_secrets(cfg: dict, loaded: dict) -> None:
    """Migre l'ancien champ `webhook_url` (v3, clair dans config.json) vers .env."""
    legacy_webhook = loaded.get("webhook_url", "")
    if legacy_webhook and not get_secret("DISCORD_WEBHOOK_URL"):
        set_secret("DISCORD_WEBHOOK_URL", legacy_webhook)
        cfg["discord"]["enabled"] = True
        logger.info("Migration: webhook Discord déplacé de config.json vers .env")

    legacy_sound = loaded.get("sound_enabled")
    if legacy_sound is not None and "sound_enabled" not in loaded.get("telegram", {}):
        cfg["telegram"]["sound_enabled"] = bool(legacy_sound)


def save_config(cfg: dict) -> None:
    """Sauvegarde config.json — ne contient jamais webhook_url (legacy) ni secrets."""
    clean = {k: v for k, v in cfg.items() if k != "webhook_url"}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)
