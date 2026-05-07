"""Offline tests for the env-var OAuth path on `GDriveSource`.

Verifies that passing the three discrete OAuth env values bypasses any
file I/O and goes directly into ``Credentials(...)``. Network refresh
+ drive build are stubbed.
"""
from __future__ import annotations

import types
from typing import Any

import pytest

from _shared.config import load_settings
from _shared.io.sources import gdrive as gdrive_module
from _shared.io.sources.gdrive import GDriveSource


_ENV_NAMES = (
    "DRIVE_FOLDER_ID", "FOLDER_ID",
    "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
    "DRIVE_CREDENTIALS_PATH", "DRIVE_TOKEN_PATH", "DRIVE_DOWNLOAD_DIR",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class _FakeCreds:
    def __init__(self, **fields: Any) -> None:
        self.fields = fields
        self.valid = True
    def refresh(self, _request):
        self.valid = True


def _stub_credentials_class(monkeypatch, captured: dict[str, Any]):
    """Patch the Credentials constructor to record the kwargs."""
    def fake_init(*args, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeCreds(**kwargs)
    monkeypatch.setattr(gdrive_module, "Credentials", fake_init)


def _stub_build(monkeypatch, captured: dict[str, Any]):
    def fake_build(name, version, *, credentials, cache_discovery):
        captured["build_args"] = (name, version, credentials, cache_discovery)
        return types.SimpleNamespace(name="fake-drive-service")
    monkeypatch.setattr(gdrive_module, "build", fake_build)


def _stub_request(monkeypatch):
    """Avoid network: patch the Request that creds.refresh uses."""
    class _NoopRequest:
        def __init__(self, *a, **k): ...
    monkeypatch.setattr(gdrive_module, "Request", _NoopRequest)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

def test_env_var_auth_skips_file_paths(clean_env, tmp_path, monkeypatch):
    """When all three OAuth env values are set, no JSON file is read."""
    captured: dict[str, Any] = {}
    _stub_credentials_class(monkeypatch, captured)
    _stub_build(monkeypatch, captured)
    _stub_request(monkeypatch)
    bogus = tmp_path / "does-not-exist.json"

    source = GDriveSource(
        folder_id="folder-X",
        credentials_path=bogus,
        token_path=bogus,
        oauth_client_id="cid",
        oauth_client_secret="csec",
        oauth_refresh_token="rtoken",
    )
    service = source._build_service()

    assert service.name == "fake-drive-service"
    assert captured["kwargs"]["client_id"] == "cid"
    assert captured["kwargs"]["client_secret"] == "csec"
    assert captured["kwargs"]["refresh_token"] == "rtoken"
    assert captured["kwargs"]["token"] is None  # forces a refresh on first use
    assert captured["kwargs"]["token_uri"] == "https://oauth2.googleapis.com/token"


def test_from_settings_env_mode(clean_env, monkeypatch):
    """Settings → from_settings → env-mode auth, end-to-end."""
    captured: dict[str, Any] = {}
    _stub_credentials_class(monkeypatch, captured)
    _stub_build(monkeypatch, captured)
    _stub_request(monkeypatch)

    s = load_settings(
        yaml_paths=[], env_file=None,
        drive_folder_id="folder-from-settings",
        google_oauth_client_id="cid-fs",
        google_oauth_client_secret="csec-fs",
        google_oauth_refresh_token="rtok-fs",
    )
    source = GDriveSource.from_settings(s)

    assert source.folder_id == "folder-from-settings"
    assert source.oauth_client_id == "cid-fs"
    assert source.oauth_refresh_token == "rtok-fs"

    source._build_service()
    assert captured["kwargs"]["refresh_token"] == "rtok-fs"


def test_from_settings_falls_back_to_file_mode_when_partial(clean_env, tmp_path, monkeypatch):
    """If only some OAuth env values are set, file mode wins (not partial env)."""
    file_calls: list[Any] = []
    def fake_build_service(cred_path, tok_path):
        file_calls.append((cred_path, tok_path))
        return types.SimpleNamespace(name="from-file-service")
    monkeypatch.setattr(gdrive_module, "build_service", fake_build_service)

    s = load_settings(
        yaml_paths=[], env_file=None,
        drive_folder_id="f",
        drive_credentials_path=tmp_path / "creds.json",
        drive_token_path=tmp_path / "token.json",
        # Only client_id set — partial; should NOT go env-mode.
        google_oauth_client_id="cid-only",
    )
    source = GDriveSource.from_settings(s)
    service = source._build_service()

    assert service.name == "from-file-service"
    assert file_calls == [(tmp_path / "creds.json", tmp_path / "token.json")]


def test_from_settings_falls_back_to_file_mode_when_all_unset(clean_env, tmp_path, monkeypatch):
    """No OAuth env values → file paths are honored."""
    file_calls: list[Any] = []
    def fake_build_service(cred_path, tok_path):
        file_calls.append((cred_path, tok_path))
        return types.SimpleNamespace(name="from-file-service")
    monkeypatch.setattr(gdrive_module, "build_service", fake_build_service)

    s = load_settings(
        yaml_paths=[], env_file=None,
        drive_folder_id="f",
        drive_credentials_path=tmp_path / "creds.json",
        drive_token_path=tmp_path / "token.json",
    )
    source = GDriveSource.from_settings(s)
    service = source._build_service()

    assert service.name == "from-file-service"


def test_from_settings_missing_folder_id(clean_env):
    s = load_settings(yaml_paths=[], env_file=None)
    with pytest.raises(RuntimeError, match="drive_folder_id"):
        GDriveSource.from_settings(s)
