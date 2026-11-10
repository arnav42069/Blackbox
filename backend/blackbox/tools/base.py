from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ToolError(BaseModel):
    kind: Literal['validation','policy_denial','transient','permanent','unavailable']
    code: str
    message: str
    retryable: bool = False
    fields: list[dict] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    previous_evidence_id: str | None = None


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
    cached_scan_id: str | None = None
    error: ToolError | None = None
    validated_arguments: dict | None = None


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
        parameters=self.argument_schema.model_json_schema()
        from .validation import EXAMPLES
        examples=[self.argument_schema.model_validate(EXAMPLES[self.name]).model_dump(exclude_unset=True)] if self.name in EXAMPLES else parameters.get('examples',[])
        if examples:
            parameters['examples']=examples
        description=self.description
        if examples:
            import json
            description+=' Example syntax only (not observed targets/files/access): '+json.dumps(examples[0],separators=(',',':'))
        return {'type': 'function', 'function': {'name': self.name, 'description': description, 'parameters': parameters}}
