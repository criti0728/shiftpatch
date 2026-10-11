"""Small, injectable SDK boundary for the explicitly bounded agent loop.

SDK reference: https://googleapis.github.io/python-genai/
No Python callable is handed to the SDK; all function requests go through our
dispatcher. The raw model Content is retained for function-call signatures.
"""
from dataclasses import dataclass, field
import inspect
import json
import os
from typing import Any, Protocol


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


class GeminiTransport:
    """Google native Gemini API; Developer API key or Cloud ADC authentication."""

    def __init__(self):
        from urllib.parse import urlparse
        from google import genai
        from google.genai import types
        self.model = os.getenv('SHIFTPATCH_GEMINI_MODEL', '').strip()
        if not self.model:
            raise ValueError('Gemini設定が不足しています: SHIFTPATCH_GEMINI_MODEL, GOOGLE_CLOUD_PROJECT (Cloud認証の場合)')
        provider = os.getenv('SHIFTPATCH_GEMINI_PROVIDER', 'auto')
        key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
        if provider == 'auto':
            provider = 'developer' if key else 'vertex'
        if provider not in {'developer', 'vertex'}:
            raise ValueError('SHIFTPATCH_GEMINI_PROVIDER は developer / vertex / auto です。')
        endpoint = os.getenv('SHIFTPATCH_GEMINI_ENDPOINT', '').strip()
        if endpoint:
            parsed = urlparse(endpoint)
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'})):
                raise ValueError('Gemini endpoint はHTTPSのベースURL（ローカルのみHTTP可）を指定してください。')
            if ':generateContent' in endpoint or '/models/' in parsed.path:
                raise ValueError('モデルの完全なURLではなくAPIのベースURLを指定してください。')
        self.last_usage = {}
        options = types.HttpOptions(
            api_version=os.getenv('SHIFTPATCH_GEMINI_API_VERSION', 'v1beta' if provider == 'developer' else 'v1'),
            base_url=endpoint or None, timeout=30_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        if provider == 'developer':
            if not key:
                raise ValueError('Gemini設定が不足しています: GEMINI_API_KEY')
            self.client = genai.Client(api_key=key, http_options=options)
        else:
            missing = [name for name in ('GOOGLE_CLOUD_PROJECT', 'GOOGLE_CLOUD_LOCATION') if not os.getenv(name)]
            if missing:
                raise ValueError('Gemini設定が不足しています: ' + ', '.join(missing))
            selector = 'enterprise' if 'enterprise' in inspect.signature(genai.Client).parameters else 'vertexai'
            self.client = genai.Client(**{selector: True}, project=os.environ['GOOGLE_CLOUD_PROJECT'],
                                       location=os.environ['GOOGLE_CLOUD_LOCATION'], http_options=options)

    async def generate(self, messages, system_instruction, declarations):
        from google.genai import types
        from google.genai.errors import APIError
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
                    temperature=0.2, max_output_tokens=4096,
                ),
            )
        except APIError as exc:
            if exc.code in (408, 429) or (exc.code and exc.code >= 500):
                raise ConnectionError("Geminiの一時的な接続エラーです。") from exc
            raise
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
