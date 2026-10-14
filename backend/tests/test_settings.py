import os

import pytest

from app.settings import GeminiSettings, agent_timeout_seconds, load_env


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for name in ("SHIFTPATCH_AI_MODE", "SHIFTPATCH_GEMINI_PROVIDER", "SHIFTPATCH_GEMINI_MODEL",
                 "GEMINI_MODEL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT",
                 "GOOGLE_CLOUD_LOCATION", "SHIFTPATCH_GEMINI_ENDPOINT", "SHIFTPATCH_GEMINI_API_VERSION",
                 "SHIFTPATCH_AI_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_environment_file_quotes_comments_and_process_priority(tmp_path, monkeypatch):
    source = tmp_path / ".env"
    source.write_text('export GEMINI_API_KEY="secret#with spaces" # comment\nGOOGLE_CLOUD_PROJECT=file-project\n', encoding="utf-8")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "process-project")
    load_env(source)
    settings = GeminiSettings.from_env()
    assert settings.project == "process-project"
    assert settings.api_key == "secret#with spaces"
    assert settings.provider == "vertex"
    assert "secret#with spaces" not in repr(settings)


def test_bad_environment_file_is_atomic_and_never_reports_a_secret(tmp_path):
    source = tmp_path / ".env"
    source.write_text('GEMINI_API_KEY=super-secret\nWRONG-NAME=hidden\n', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_env(source)
    assert "super-secret" not in str(error.value)
    assert "GEMINI_API_KEY" not in os.environ


def test_key_only_configuration_selects_developer(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "server-secret")
    settings = GeminiSettings.from_env()
    assert settings.provider == "developer"
    assert settings.api_version == "v1beta"
    settings.require_credentials()


def test_vertex_is_selected_for_a_project_even_with_an_unrelated_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gcp-project")
    monkeypatch.setenv("GOOGLE_API_KEY", "unrelated-key")
    settings = GeminiSettings.from_env()
    assert settings.provider == "vertex"
    assert settings.location == "global"
    assert settings.api_version == "v1"
    settings.require_credentials()


def test_explicit_developer_selection_overrides_a_project(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "gcp-project")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("SHIFTPATCH_GEMINI_PROVIDER", "developer")
    assert GeminiSettings.from_env().provider == "developer"


@pytest.mark.parametrize("endpoint", ["https://example.com/models/gemini:generateContent",
                                    "https://example.com/openai/v1", "http://example.com",
                                    "https://secret@example.com", "https://example.com?key=secret"])
def test_invalid_endpoint_does_not_expose_credentials(monkeypatch, endpoint):
    monkeypatch.setenv("SHIFTPATCH_GEMINI_ENDPOINT", endpoint)
    with pytest.raises(ValueError) as error:
        GeminiSettings.from_env()
    assert "secret" not in str(error.value)


def test_missing_credentials_are_reported_by_provider(monkeypatch):
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        GeminiSettings.from_env().require_credentials()
    monkeypatch.setenv("SHIFTPATCH_GEMINI_PROVIDER", "developer")
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiSettings.from_env().require_credentials()


@pytest.mark.parametrize("value", ["", "0", "-5", "181", "inf", "nan", "text"])
def test_invalid_agent_budget_is_rejected(monkeypatch, value):
    monkeypatch.setenv("SHIFTPATCH_AI_TIMEOUT_SECONDS", value)
    with pytest.raises(ValueError, match="SHIFTPATCH_AI_TIMEOUT_SECONDS"):
        agent_timeout_seconds()


def test_agent_budget_configuration_and_default(monkeypatch):
    assert agent_timeout_seconds() == 40
    assert agent_timeout_seconds(default=0.03) == 0.03
    monkeypatch.setenv("SHIFTPATCH_AI_TIMEOUT_SECONDS", "120")
    assert agent_timeout_seconds() == 120
