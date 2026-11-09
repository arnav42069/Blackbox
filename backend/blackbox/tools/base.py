from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ToolResult(BaseModel):
    ok: bool
    exit_code: int | None = None
    stdout: str = ''
    stderr: str = ''
    summary: str = ''
    evidence: list[dict] = Field(default_factory=list)
    elapsed_ms: int = 0
    truncated: bool = False
    denied_reason: str | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    argument_schema: type[BaseModel]
    risk: str
    requires_target_scope: bool
    timeout_s: int
    run: Callable[..., Awaitable[ToolResult]]
    binary: str | None = None

    def schema(self):
        return {'type': 'function', 'function': {'name': self.name, 'description': self.description, 'parameters': self.argument_schema.model_json_schema()}}
