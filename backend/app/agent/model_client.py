"""Small, injectable SDK boundary for the explicitly bounded agent loop.

SDK reference: https://googleapis.github.io/python-genai/
No Python callable is handed to the SDK; all function requests go through our
dispatcher. The raw model Content is retained for function-call signatures.
"""
from dataclasses import dataclass, field
import inspect
import json
from typing import Any, Protocol

from app.settings import GeminiSettings


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str | None = None


@dataclass
class ModelReply:
    calls: list[ToolCall] = field(default_factory=list)
    final: dict | None = None
    content: Any = None
    usage: dict[str, int] = field(default_factory=dict)


class ModelTransport(Protocol):
    async def generate(self, messages: list, system_instruction: str,
                       declarations: list[dict]) -> ModelReply: ...


class GeminiProviderError(Exception):
    """Public diagnostics assembled locally, never from a provider response."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


def _api_error(status: int | None) -> GeminiProviderError:
    diagnostics = {
        400: ('model_request_invalid', 'Geminiへのリクエストが拒否されました。モデル・APIバージョン・接続先を確認してください。'),
        401: ('model_authentication_failed', 'Gemini認証が無効です。APIキー、または gcloud auth application-default login の認証を更新してください。'),
        403: ('model_permission_denied', 'Geminiへのアクセスが拒否されました。認証アカウント・プロジェクトの Vertex AI 権限・APIの有効化・キーの制限を確認してください。'),
        404: ('model_not_found', '指定したGeminiモデルまたは接続先が見つかりません。モデルID・リジョン・APIのベースURLを確認してください。'),
        429: ('model_rate_limited', 'Geminiの利用制限に達しました。プロジェクトのクォータ・課金・クレジット残高を確認して再実行してください。'),
    }
    code, message = diagnostics.get(status, ('model_unavailable', 'Geminiサービスに接続できません。接続先とサービスの状態を確認して再実行してください。'))
    return GeminiProviderError(code, message, http_status=status,
                               retryable=status in (408, 429) or bool(status and status >= 500))


class GeminiTransport:
    """Google native Gemini API; Developer API key or Cloud ADC authentication."""

    def __init__(self):
        from google import genai
        from google.genai import types
        settings = GeminiSettings.from_env()
        settings.require_credentials()
        self.model = settings.model
        self.provider = settings.provider
        self.last_usage = {}
        options = types.HttpOptions(
            api_version=settings.api_version,
            base_url=settings.endpoint or None, timeout=30_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        # Select both paths explicitly: SDK environment flags must not reroute a
        # configured Developer key to Vertex, or vice versa.
        selector = 'enterprise' if 'enterprise' in inspect.signature(genai.Client).parameters else 'vertexai'
        if settings.provider == 'developer':
            self.client = genai.Client(**{selector: False}, api_key=settings.api_key, http_options=options)
        else:
            self.client = genai.Client(**{selector: True}, project=settings.project,
                                       location=settings.location, http_options=options)

    async def generate(self, messages, system_instruction, declarations):
        from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
        from google.genai import types
        from google.genai.errors import APIError
        from httpx import RequestError
        self.last_usage = {}
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=[types.Content.model_validate(item) if isinstance(item, dict) else item
                          for item in messages],
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[types.Tool(function_declarations=[
                        types.FunctionDeclaration(**item) for item in declarations
                    ])],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    # The agent delegates arithmetic to deterministic tools. A
                    # supported low thinking level leaves time for tool results
                    # and validation inside the bounded interactive request.
                    thinking_config=types.ThinkingConfig(thinking_level='LOW', include_thoughts=False)
                    if self.model.startswith(('gemini-3', 'gemini-4')) else None,
                    temperature=0.2, max_output_tokens=4096,
                ),
            )
        except APIError as exc:
            raise _api_error(exc.code) from exc
        except (DefaultCredentialsError, RefreshError) as exc:
            raise GeminiProviderError(
                'model_authentication_failed',
                'GCP認証を利用できません。gcloud auth application-default login を実行するか、実行サービスの認証を確認してください。',
            ) from exc
        except (TransportError, RequestError) as exc:
            raise GeminiProviderError(
                'model_connection_failed', 'GeminiまたはGCP認証サービスに接続できません。ネットワークと接続先を確認してください。',
                retryable=True,
            ) from exc
        metadata = response.usage_metadata
        self.last_usage = {
            name: int(getattr(metadata, name, 0) or 0)
            for name in ("prompt_token_count", "candidates_token_count", "thoughts_token_count", "total_token_count")
        } if metadata else {}
        if not response.candidates or not response.candidates[0].content:
            raise ValueError("Geminiから有効な応答がありません。")
        content = response.candidates[0].content
        calls = [ToolCall(part.function_call.name, dict(part.function_call.args or {}),
                          part.function_call.id)
                 for part in content.parts or [] if part.function_call]
        if calls:
            return ModelReply(calls=calls, content=content, usage=self.last_usage)
        raw = "".join(part.text or "" for part in content.parts or [] if not part.thought).strip()
        if raw.startswith(chr(96) * 3) and "\n" not in raw:
            raise ValueError("最終JSONのコードブロックが不完全です。")
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        if len(raw) > 24_000:
            raise ValueError("Gemini応答がサイズ上限を超えました。")
        final = json.loads(raw)
        if not isinstance(final, dict):
            raise ValueError("Geminiの最終応答はJSON objectである必要があります。")
        return ModelReply(final=final, content=content, usage=self.last_usage)

    async def aclose(self):
        await self.client.aio.aclose()
        self.client.close()
