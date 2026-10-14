import asyncio
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from app.settings import GeminiSettings


def check_script(monkeypatch, *, text="OK", failure=None):
    from app.agent import model_client
    settings = GeminiSettings("gemini", "developer", "gemini-3.8-flash", "test-secret", "", "global", "", "v1beta")
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/check_gemini.py"))
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "load_env", lambda: None)
    monkeypatch.setattr(GeminiSettings, "from_env", classmethod(lambda cls: settings))
    observations = {"closed": False}

    class Transport:
        def __init__(self):
            self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.generate)))

        async def generate(self, **kwargs):
            observations["model"] = kwargs["model"]
            if failure:
                raise failure
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
                SimpleNamespace(text=text, thought=False)
            ]))])

        async def aclose(self):
            observations["closed"] = True

    monkeypatch.setattr(model_client, "GeminiTransport", Transport)
    return main, observations


def test_check_script_uses_shared_model_and_redacts_an_echoed_key(monkeypatch, capsys):
    main, observations = check_script(monkeypatch, text="OK test-secret")
    assert asyncio.run(main()) == 0
    output = capsys.readouterr().out
    assert "test-secret" not in output and "[숨김]" in output
    assert observations == {"closed": True, "model": "gemini-3.8-flash"}


def test_check_script_does_not_label_an_empty_response_as_success(monkeypatch, capsys):
    main, observations = check_script(monkeypatch, text="")
    assert asyncio.run(main()) == 1
    assert "성공:" not in capsys.readouterr().out
    assert observations["closed"]


def test_check_script_suppresses_sdk_validation_details(monkeypatch, capsys):
    main, observations = check_script(monkeypatch, failure=ValueError("URL key=test-secret"))
    assert asyncio.run(main()) == 2
    assert "test-secret" not in capsys.readouterr().out
    assert observations["closed"]


def test_check_script_permission_error_is_safe_and_actionable(monkeypatch, capsys):
    from google.genai.errors import ClientError
    error = ClientError(403, {"error": {"message": "blocked test-secret", "status": "PERMISSION_DENIED"}})
    main, observations = check_script(monkeypatch, failure=error)
    assert asyncio.run(main()) == 1
    output = capsys.readouterr().out
    assert "model_permission_denied" in output and "HTTP 403" in output
    assert "test-secret" not in output
    assert observations["closed"]
