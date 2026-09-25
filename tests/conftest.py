"""Shared isolation: no test touches the real data (var/)."""

from __future__ import annotations

import pytest

from app import activation, auth, security, visits


@pytest.fixture(autouse=True)
def _reset_security(monkeypatch):
    """In-memory blocks / counters reset; no delay after a failure."""
    security.reset()
    monkeypatch.setattr(security, "FAILED_LOGIN_DELAY", 0)
    yield
    security.reset()


@pytest.fixture(autouse=True)
def _isolate_data(tmp_path, monkeypatch):
    monkeypatch.setattr(visits, "DB_PATH", tmp_path / "auth_log.sqlite")
    monkeypatch.setattr(auth, "SECRET_FILE", tmp_path / "auth_secret")
    monkeypatch.setattr(activation, "DB_PATH", tmp_path / "activation.sqlite")
    monkeypatch.setattr(activation, "OP_PASSWORD_FILE", tmp_path / "activation_password")
    monkeypatch.setattr(activation, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(activation, "SETTINGS_FILE", tmp_path / "activation_settings.json")
    monkeypatch.setattr(activation, "IMPORT_TMP_DIR", tmp_path / "import")
    monkeypatch.setattr(activation, "qrz_client", lambda: None)  # never the real QRZ in tests
