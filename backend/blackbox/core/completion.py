"""A bounded completion review is a control decision, never an executable tool."""
import re
from typing import Literal

from pydantic import Field, model_validator
from ..tools.base import Arguments


class CompletionReview(Arguments):
    decision: Literal['complete', 'continue']
    reason: str = Field(min_length=1, max_length=600)
    remaining_steps: list[str] = Field(max_length=8)

    @model_validator(mode='after')
    def consistent(self):
        if self.decision == 'complete' and self.remaining_steps:
            raise ValueError('A completed task cannot have remaining steps')
        return self


REVIEW_TOOL = {'type': 'function', 'function': {'name': 'agent.review_completion',
    'description': 'Assess the proposed answer against the original user task and verified observations. Complete only when all requested work is done or a genuine access/scope/tool limitation is evidenced and clearly explained. Otherwise continue.',
    'parameters': CompletionReview.model_json_schema()}}

REVIEW_INSTRUCTION = '''Controller completion review: the preceding assistant answer is a draft, not completion.
Use ONLY agent.review_completion to assess it against the original user request and actual tool observations.
Check every factual hardware/command claim against observed results, and all requested steps for completion.
Future promises such as "I'll run those commands next" are unfinished work, even after one successful tool.
Do not invent missing tool results. Do not demand unrelated extra work or repeated checks already measured.
If work remains, decision=continue and list concrete remaining steps. If evidence is missing, request the relevant registered tool.
A truthful answer to an ordinary informational request may be complete without any tools.
Return the structured review call only, no private reasoning. This control tool cannot execute actions or grant authorization.'''


def unfinished_promise(text):
    # Conservative veto, not a prose-to-command interpreter. Ignore quoted/code examples.
    text = re.sub(r'```.*?```', '', text, flags=re.S)
    text = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>'))
    return bool(re.search(r"\b(?:I|we)(?:['’](?:ll|m)|\s+(?:will|shall|am going to|need to|still need to|must))\s+(?:now\s+|also\s+|next\s+)?(?:run|execute|check|verify|inspect|scan|probe|find|read|test|gather|collect|confirm|continue|use|perform|retrieve|search|investigate|look)\b", text, re.I))
