"""Offline tests for `init` and `doctor` workspace helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from _shared.config import (
    Settings,
    check_config,
    doctor_exit_code,
    format_check_results,
    format_init_result,
    init_workspace,
)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _seed_env_example(root: Path) -> Path:
    src = root / ".env.example"
    src.write_text("JIRA_HOST=\nJIRA_TOKEN=\n", encoding="utf-8")
    return src


def test_init_creates_env_and_local_files(tmp_path: Path) -> None:
    _seed_env_example(tmp_path)
    result = init_workspace(root=tmp_path)
    assert result.env_created
    assert (tmp_path / ".env").read_text() == "JIRA_HOST=\nJIRA_TOKEN=\n"
    assert (tmp_path / "data" / "local_files").is_dir()
    assert (tmp_path / "data" / "local_files") in result.dirs_created


def test_init_does_not_clobber_existing_env(tmp_path: Path) -> None:
    _seed_env_example(tmp_path)
    existing = tmp_path / ".env"
    existing.write_text("JIRA_TOKEN=already-set\n", encoding="utf-8")

    result = init_workspace(root=tmp_path)

    assert result.env_created is False
    assert existing.read_text() == "JIRA_TOKEN=already-set\n"


def test_init_reports_dirs_that_already_exist(tmp_path: Path) -> None:
    _seed_env_example(tmp_path)
    pre_existing = tmp_path / "data" / "local_files"
    pre_existing.mkdir(parents=True)

    result = init_workspace(root=tmp_path)

    assert pre_existing in result.dirs_existed
    assert pre_existing not in result.dirs_created


def test_init_raises_when_env_example_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        init_workspace(root=tmp_path)


def test_format_init_result_mentions_next_steps(tmp_path: Path) -> None:
    _seed_env_example(tmp_path)
    result = init_workspace(root=tmp_path)
    text = format_init_result(result)
    assert "JIRA_HOST" in text
    assert "NVIDIA_API_KEY" in text
    assert "DRIVE_FOLDER_ID" in text


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _settings(**overrides) -> Settings:
    """Build a Settings with all fields explicit so the test is hermetic
    (no reads from the host's .env)."""
    base: dict = {
        "jira_host": None,
        "jira_project_key": None,
        "jira_auth_mode": "bearer",
        "jira_user_email": None,
        "jira_token": None,
        "autodev_token": None,
        "nvidia_api_key": None,
        "drive_folder_id": None,
        "google_oauth_client_id": None,
        "google_oauth_client_secret": None,
        "google_oauth_refresh_token": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_doctor_reports_all_required_missing() -> None:
    checks = check_config(_settings())
    by_name = {c.name: c.status for c in checks}
    assert by_name["JIRA_HOST"] == "error"
    assert by_name["JIRA_PROJECT_KEY"] == "error"
    assert by_name["JIRA_TOKEN"] == "error"
    assert by_name["NVIDIA_API_KEY"] == "error"
    assert doctor_exit_code(checks) == 1


def test_doctor_passes_with_minimal_config() -> None:
    checks = check_config(_settings(
        jira_host="jirasw.example.com",
        jira_project_key="PROJ",
        jira_token="abc",
        nvidia_api_key="def",
    ))
    statuses = [c.status for c in checks]
    assert "error" not in statuses
    assert doctor_exit_code(checks) == 0


def test_doctor_warns_when_drive_unset_but_does_not_fail() -> None:
    checks = check_config(_settings(
        jira_host="x", jira_project_key="P", jira_token="t", nvidia_api_key="k",
    ))
    drive = next(c for c in checks if c.name == "Drive")
    assert drive.status == "warn"
    assert doctor_exit_code(checks) == 0


def test_doctor_errors_on_partial_drive_config() -> None:
    checks = check_config(_settings(
        jira_host="x", jira_project_key="P", jira_token="t", nvidia_api_key="k",
        drive_folder_id="folder-uuid",
        google_oauth_client_id="client",
        # missing client_secret + refresh_token → 2/4
    ))
    drive = next(c for c in checks if c.name == "Drive")
    assert drive.status == "error"
    assert "2/4" in drive.detail
    assert doctor_exit_code(checks) == 1


def test_doctor_errors_when_basic_auth_missing_email() -> None:
    checks = check_config(_settings(
        jira_host="x", jira_project_key="P", jira_token="t",
        jira_auth_mode="basic", nvidia_api_key="k",
    ))
    email_check = next(c for c in checks if c.name == "JIRA_USER_EMAIL")
    assert email_check.status == "error"
    assert doctor_exit_code(checks) == 1


def test_doctor_errors_on_invalid_auth_mode() -> None:
    checks = check_config(_settings(
        jira_host="x", jira_project_key="P", jira_token="t",
        jira_auth_mode="oauth", nvidia_api_key="k",
    ))
    mode_check = next(c for c in checks if c.name == "JIRA_AUTH_MODE")
    assert mode_check.status == "error"


def test_format_check_results_includes_all_check_names() -> None:
    checks = check_config(_settings())
    text = format_check_results(checks)
    for name in ("JIRA_HOST", "JIRA_PROJECT_KEY", "JIRA_TOKEN", "NVIDIA_API_KEY", "Drive"):
        assert name in text
    assert "Result:" in text
