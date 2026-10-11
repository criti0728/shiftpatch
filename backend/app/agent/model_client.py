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
    """Gemini through the Google Cloud platform, using ADC rather than an API key."""

    def __init__(self):
        keys = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "SHIFTPATCH_GEMINI_MODEL")
        missing = [key for key in keys if not os.environ.get(key, "").strip()]
        if missing:
            raise ValueError("Gemini設定が不足しています: " + ", ".join(missing))
        from google import genai
        from google.genai import types
        self.model = os.environ["SHIFTPATCH_GEMINI_MODEL"]
        self.last_usage = {}
        # Both SDK generations still address the Google Cloud model platform.
        selector = "enterprise" if "enterprise" in inspect.signature(genai.Client).parameters else "vertexai"
        self.client = genai.Client(
            **{selector: True}, project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ["GOOGLE_CLOUD_LOCATION"],
            http_options=types.HttpOptions(
                api_version="v1", timeout=30_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

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
