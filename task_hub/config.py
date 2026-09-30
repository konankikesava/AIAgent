from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    data_dir: Path
    provider: str = "ollama"
    model: str = ""
    model_url: str = "http://127.0.0.1:11434"
    model_key: str = ""
    github_token: str = ""
    github_repos: tuple[str, ...] = ()
    github_writes: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
    workers: int = 2
    max_steps: int = 10
    http_timeout: int = 60

    @classmethod
    def from_env(cls, demo=False):
        return cls(
            data_dir=Path(os.getenv("HUB_DATA_DIR", "task_hub/data")).resolve(),
            provider="demo" if demo else os.getenv("MODEL_PROVIDER", "ollama"),
            model=os.getenv("MODEL_NAME", ""),
            model_url=os.getenv("MODEL_URL", "http://127.0.0.1:11434").rstrip("/"),
            model_key=os.getenv("MODEL_API_KEY", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            github_repos=tuple(x.strip() for x in os.getenv("GITHUB_ALLOWED_REPOS", "").split(",") if x.strip()),
            github_writes=os.getenv("GITHUB_WRITES_ENABLED", "false").lower() == "true",
            host=os.getenv("HUB_HOST", "127.0.0.1"),
            port=int(os.getenv("HUB_PORT", "8765")),
            auth_token=os.getenv("HUB_AUTH_TOKEN", ""),
            allowed_hosts=tuple(x.strip() for x in os.getenv("HUB_ALLOWED_HOSTS", "localhost,127.0.0.1,::1").split(",") if x.strip()),
            workers=int(os.getenv("HUB_WORKERS", "2")),
        )

    def validate(self):
        if self.provider not in ("ollama", "compatible", "demo"):
            raise ValueError("MODEL_PROVIDER must be ollama, compatible, or demo")
        if self.provider != "demo" and not self.model:
            raise ValueError("Set MODEL_NAME to a model available on your endpoint, or use --demo")
        url = urlsplit(self.model_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("MODEL_URL must be an HTTP(S) base URL without embedded credentials")
        if self.host not in ("127.0.0.1", "localhost", "::1") and len(self.auth_token) < 24:
            raise ValueError("Network binding requires HUB_AUTH_TOKEN with at least 24 characters")
        if not 1 <= self.workers <= 4 or not 1 <= self.port <= 65535:
            raise ValueError("Use 1–4 workers and a valid TCP port")
        if not self.allowed_hosts:
            raise ValueError("HUB_ALLOWED_HOSTS must contain at least one hostname")

    def public(self):
        return {
            "provider": self.provider, "model": self.model or "Offline demonstration",
            "github_repos": list(self.github_repos),
            "github_writes": self.github_writes and self.provider != "demo",
            "workers": self.workers, "max_steps": self.max_steps,
        }
