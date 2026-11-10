from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = "qwen3.5:4b"
    mode: Literal["auto_safe", "manual"] = "auto_safe"
    num_ctx: int = Field(default=8192, ge=2048, le=16384)
    temperature: float = Field(default=0.2, ge=0, le=1)
    keep_alive: str = "20m"
    think: bool = False
    max_tool_calls: int = Field(default=20, ge=1, le=20)
    max_turns: int = Field(default=12, ge=1, le=12)
    max_wall_seconds: int = Field(default=300, ge=5, le=300)
    retention_days: int = Field(default=30, ge=1, le=365)
    visual_effects: bool = False
    vision_model: str | None = None
    scan_freshness_seconds: int = Field(default=900, ge=0, le=86400)
    http_rate: int = Field(default=2, ge=1, le=5)
    http_concurrency: int = Field(default=1, ge=1, le=2)
    discovery_timeout_seconds: int = Field(default=40, ge=1, le=40)

    @field_validator("model")
    @classmethod
    def model_name(cls, value: str) -> str:
        if not value or len(value) > 160 or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid model name")
        return value

    @field_validator("keep_alive")
    @classmethod
    def keepalive(cls, value: str) -> str:
        if value not in {"0", "5m", "10m", "20m", "30m"}:
            raise ValueError("Choose 0, 5m, 10m, 20m or 30m")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    port: int = Field(default=8765, ge=1024, le=65535)
    data_dir: Path = Field(default_factory=lambda: Path(os.environ.get("BLACKBOX_DATA_DIR", Path.home() / ".local/share/blackbox")))
    config_dir: Path = Field(default_factory=lambda: Path(os.environ.get("BLACKBOX_CONFIG_DIR", Path.home() / ".config/blackbox")))
    ollama_url: str = Field(default_factory=lambda: os.environ.get("BLACKBOX_OLLAMA_URL", "http://127.0.0.1:11434"))
    frontend_dir: Path = ROOT / "frontend/dist"
    prompt_path: Path = ROOT / "prompts/agent_system.txt"
    prefs: Preferences = Field(default_factory=Preferences)

    @field_validator("ollama_url")
    @classmethod
    def loopback_ollama(cls, value: str) -> str:
        u = urlsplit(value)
        if u.scheme != "http" or u.hostname not in {"127.0.0.1", "::1"} or u.username or u.password or u.path not in {"", "/"} or u.query or u.fragment:
            raise ValueError("Ollama must use a numeric loopback HTTP URL")
        return value.rstrip("/")

    def prepare(self) -> None:
        for path in (self.data_dir, self.data_dir / "attachments", self.data_dir / "artifacts", self.data_dir / "workspace"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        config = self.config_dir / "config.yaml"
        if config.exists():
            self.prefs = Preferences.model_validate(yaml.safe_load(config.read_text()) or {})

    def save_preferences(self, prefs: Preferences) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.config_dir / "config.yaml"
        from .storage.files import atomic_write
        atomic_write(target, yaml.safe_dump(prefs.model_dump()).encode())
        self.prefs = prefs
