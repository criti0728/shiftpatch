#!/usr/bin/env python3
"""앱과 같은 .env/SDK/인증으로 Gemini 확인: python3 scripts/check_gemini.py"""
import asyncio
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.settings import GeminiSettings, load_env


async def main():
    transport = None
    try:
        load_env()
        settings = GeminiSettings.from_env()
        settings.require_credentials()
    except ValueError as error:
        print(".env 설정 오류:", error)
        return 2
    try:
        from app.agent.model_client import GeminiTransport, _api_error
        from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
        from google.genai import types
        from google.genai.errors import APIError
        from httpx import RequestError
        transport = GeminiTransport()
        print("Gemini 연결 확인:", settings.provider, settings.model)
        if settings.provider == "vertex":
            print(f"프로젝트: {settings.project} · 리전: {settings.location} · 인증: ADC")
        try:
            async with asyncio.timeout(settings.request_timeout_seconds + 5):
                response = await transport.client.aio.models.generate_content(
                    model=settings.model, contents="Reply with only OK.",
                    config=types.GenerateContentConfig(
                        temperature=0, max_output_tokens=1024,
                        thinking_config=types.ThinkingConfig(thinking_level="LOW")
                        if settings.model.startswith(("gemini-3", "gemini-4")) else None,
                    ),
                )
        except APIError as error:
            diagnostic = _api_error(error.code)
            print(f"실패 (HTTP {error.code}, {diagnostic.code}): {diagnostic}")
            return 1
        except (DefaultCredentialsError, RefreshError):
            print("GCP 인증을 확인하세요. gcloud auth application-default login으로 프로젝트 소유자 계정을 인증하세요.")
            return 2
        except (TransportError, RequestError, TimeoutError):
            print("Gemini 또는 GCP 인증 서버에 연결할 수 없거나 응답 시간이 초과됐습니다.")
            return 1
        text = "".join(part.text or "" for candidate in response.candidates or []
                       if candidate.content for part in candidate.content.parts or []
                       if not part.thought).strip()
        if not text:
            print("실패: Gemini 응답에서 생성된 텍스트를 확인할 수 없습니다.")
            return 1
        if settings.api_key:
            text = text.replace(settings.api_key, "[숨김]")
        print("성공: 앱과 같은 설정으로 Gemini가 실제 응답했습니다.")
        print("응답:", text)
        return 0
    except ImportError:
        print("Gemini SDK 설치가 필요합니다: .venv/bin/python -m pip install -e './backend[gemini]'")
        return 2
    except ValueError:
        print("Gemini 접속 설정 또는 응답 형식을 확인할 수 없습니다. 모델·provider·endpoint를 확인하세요.")
        return 2
    except Exception:
        # Credential construction failures must not print a token/key/SDK URL.
        print("Gemini 연결 초기화에 실패했습니다. .env와 GCP ADC 인증을 확인하세요.")
        return 2
    finally:
        if transport is not None:
            try:
                await asyncio.wait_for(transport.aclose(), timeout=1)
            except Exception:
                pass


if __name__ == "__main__":
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file() and Path(sys.prefix) != ROOT / ".venv":
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n테스트를 취소했습니다.")
        sys.exit(130)
