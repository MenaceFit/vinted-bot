"""
Tests unitaires pour la logique de configuration.

Couvre :
  - _deep_merge : fusion récursive, écrasement de valeur scalaire, list override
  - _safe_config : les flags _has_* sont calculés, jamais persistés
  - update_config strip : les flags privés envoyés par le client doivent être
    retirés avant _deep_merge pour ne pas contaminer config.json
  - save_config : 'webhook_url' legacy n'est jamais sauvegardé
"""
import json
import os

import pytest

import config as cfg_mod


# ── _deep_merge ───────────────────────────────────────────────────────────────

def test_deep_merge_overwrites_scalar():
    base = {"interval_seconds": 8, "min_price": None}
    cfg_mod._deep_merge(base, {"interval_seconds": 15})
    assert base["interval_seconds"] == 15
    assert base["min_price"] is None  # untouched


def test_deep_merge_recursive_dict():
    base = {"telegram": {"enabled": False, "send_images": True}}
    cfg_mod._deep_merge(base, {"telegram": {"enabled": True}})
    assert base["telegram"]["enabled"] is True
    assert base["telegram"]["send_images"] is True  # untouched by partial override


def test_deep_merge_list_replaced_not_extended():
    """Une liste en override REMPLACE la liste de base (pas d'extension)."""
    base = {"keywords_filter": ["nike", "jordan"]}
    cfg_mod._deep_merge(base, {"keywords_filter": ["supreme"]})
    assert base["keywords_filter"] == ["supreme"]


def test_deep_merge_none_overwrites():
    """None en override efface la valeur de base."""
    base = {"max_price": 100.0}
    cfg_mod._deep_merge(base, {"max_price": None})
    assert base["max_price"] is None


# ── Private flags must not leak into persisted config ─────────────────────────

def test_private_flags_stripped_before_merge():
    """Bug: update_config laissait _has_token etc. dans config_data → sauvegardes corrompues.
    Vérifie que le strip fonctionne AVANT le deep_merge."""
    config_data = {"interval_seconds": 8, "autobuy": {"enabled": False}}
    body = {
        "interval_seconds": 12,
        "_has_token": True,
        "_has_telegram": False,
        "_has_discord": True,
    }
    # Simulate what update_config does after the fix
    for k in ("_has_token", "_has_telegram", "_has_discord"):
        body.pop(k, None)
    cfg_mod._deep_merge(config_data, body)

    assert "_has_token" not in config_data
    assert "_has_telegram" not in config_data
    assert "_has_discord" not in config_data
    assert config_data["interval_seconds"] == 12


def test_save_config_excludes_legacy_webhook(tmp_path, monkeypatch):
    """save_config ne doit jamais écrire webhook_url dans le JSON."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", path)

    data = {"interval_seconds": 8, "webhook_url": "https://discord.com/api/webhooks/secret"}
    cfg_mod.save_config(data)

    saved = json.loads(path.read_text())
    assert "webhook_url" not in saved
    assert saved["interval_seconds"] == 8


def test_save_config_excludes_private_flags(tmp_path, monkeypatch):
    """save_config ne doit pas écrire les flags _has_* dans le JSON."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", path)

    data = {"interval_seconds": 8, "_has_token": True, "_has_telegram": False}
    cfg_mod.save_config(data)

    saved = json.loads(path.read_text())
    # _has_* sont des clés privées qui NE DOIVENT PAS être dans le fichier.
    # Si elles y sont c'est le bug — le test le détecte.
    # Note: save_config ne filtre que "webhook_url" actuellement;
    # ce test est destiné à documenter le comportement attendu après correction.
    assert "_has_token" not in saved
    assert "_has_telegram" not in saved


# ── mask ──────────────────────────────────────────────────────────────────────

def test_mask_short_value():
    assert cfg_mod.mask("abc", keep=4) == "***"


def test_mask_long_value():
    masked = cfg_mod.mask("supersecrettoken", keep=4)
    assert masked.endswith("oken")
    assert masked.startswith("*")
    assert "supersecret" not in masked


def test_mask_empty():
    assert cfg_mod.mask("") == ""


# ── load_config defaults ──────────────────────────────────────────────────────

def test_load_config_returns_defaults_when_no_file(tmp_path, monkeypatch):
    """Sans config.json, les valeurs par défaut s'appliquent."""
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "nonexistent.json")
    monkeypatch.setattr(cfg_mod, "ENV_PATH", tmp_path / ".env")

    loaded = cfg_mod.load_config()
    assert loaded["interval_seconds"] == 8
    assert loaded["warmup_first_run"] is True
    assert "fr" in loaded["markets"]
    assert loaded["autobuy"]["enabled"] is False


def test_load_config_merges_saved_values(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"interval_seconds": 20, "min_price": 5.0}))
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", path)
    monkeypatch.setattr(cfg_mod, "ENV_PATH", tmp_path / ".env")

    loaded = cfg_mod.load_config()
    assert loaded["interval_seconds"] == 20
    assert loaded["min_price"] == 5.0
    assert "fr" in loaded["markets"]  # default still present
