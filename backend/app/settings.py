"""Server-only environment loading and shared Gemini connection settings."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


def agent_timeout_seconds(default: float = 40) -> float:
    raw = os.getenv("SHIFTPATCH_AI_TIMEOUT_SECONDS")
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError("SHIFTPATCH_AI_TIMEOUT_SECONDS は10〜180秒で指定してください。") from None
    if not 10 <= value <= 180:
        raise ValueError("SHIFTPATCH_AI_TIMEOUT_SECONDS は10〜180秒で指定してください。")
    return value


def gemini_request_timeout_seconds(default: float = 60) -> float:
    """Bound each SDK request independently of the entire agent execution."""
    raw = os.getenv("SHIFTPATCH_GEMINI_REQUEST_TIMEOUT_SECONDS")
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        raise ValueError("SHIFTPATCH_GEMINI_REQUEST_TIMEOUT_SECONDS は5〜120秒で指定してください。") from None
    if not 5 <= value <= 120:
        raise ValueError("SHIFTPATCH_GEMINI_REQUEST_TIMEOUT_SECONDS は5〜120秒で指定してください。")
    return value


def load_env(path: str | Path | None = None) -> None:
    """Read .env without executing shell code; existing process values win."""
    source = Path(path) if path is not None else ROOT / ".env"
    if not source.is_file():
        return
    values = {}
    for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, separator, raw = line.partition("=")
        name = name.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"{source.name}:{number}: expected NAME=value")
        try:
            parsed = shlex.split(raw, comments=True)
        except ValueError:
            raise ValueError(f"{source.name}:{number}: invalid quoted value") from None
        if len(parsed) > 1:
            raise ValueError(f"{source.name}:{number}: quote values containing spaces")
        values[name] = parsed[0] if parsed else ""
    # A malformed file never leaves a partially loaded process environment.
    for name, value in values.items():
        os.environ.setdefault(name, value)


@dataclass(frozen=True)
class GeminiSettings:
    mode: str
    provider: str
    model: str
    api_key: str = field(repr=False)
    project: str
    location: str
    endpoint: str
    api_version: str
    request_timeout_seconds: float = 60

    @classmethod
    def from_env(cls) -> "GeminiSettings":
        mode = os.getenv("SHIFTPATCH_AI_MODE", "calculation_only").strip()
        if mode not in {"calculation_only", "gemini"}:
            raise ValueError("SHIFTPATCH_AI_MODE は calculation_only または gemini です。")
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        provider = os.getenv("SHIFTPATCH_GEMINI_PROVIDER", "auto").strip()
        if provider == "auto":
            provider = "vertex" if project or not key else "developer"
        if provider not in {"developer", "vertex"}:
            raise ValueError("SHIFTPATCH_GEMINI_PROVIDER は developer / vertex / auto です。")
        model = (os.getenv("SHIFTPATCH_GEMINI_MODEL") or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
            raise ValueError("SHIFTPATCH_GEMINI_MODEL はモデルIDを指定してください。")
        endpoint = os.getenv("SHIFTPATCH_GEMINI_ENDPOINT", "").strip().rstrip("/")
        if endpoint:
            parsed = urlparse(endpoint)
            local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or (parsed.scheme != "https" and not local_http)):
                raise ValueError("Gemini endpoint はHTTPSのベースURL（ローカルのみHTTP可）を指定してください。")
            if ":generateContent" in endpoint or "/models/" in parsed.path or "/openai" in parsed.path:
                raise ValueError("GeminiネイティブAPIのベースURLを指定してください。")
        version = os.getenv("SHIFTPATCH_GEMINI_API_VERSION", "v1beta" if provider == "developer" else "v1").strip()
        if not re.fullmatch(r"v[0-9]+(?:alpha|beta)?[0-9]*", version):
            raise ValueError("SHIFTPATCH_GEMINI_API_VERSION を確認してください。")
        return cls(mode, provider, model, key, project,
                   os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip() or "global", endpoint, version,
                   gemini_request_timeout_seconds())

    def require_credentials(self) -> None:
        if self.provider == "developer" and not self.api_key:
            raise ValueError("Gemini設定が不足しています: GEMINI_API_KEY")
        if self.provider == "vertex" and not self.project:
            raise ValueError("Gemini設定が不足しています: GOOGLE_CLOUD_PROJECT")

    def description(self) -> str:
        if self.mode != "gemini":
            return "calculation_only"
        destination = f"{self.project} / {self.location}" if self.provider == "vertex" else "API key"
        return f"gemini / {self.provider} / {self.model} / {destination}"
