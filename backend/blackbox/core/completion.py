"""A bounded completion review is a control decision, never an executable tool."""
import re
from typing import Literal

from pydantic import Field, model_validator
from ..tools.base import Arguments


_LOCAL_FILE = re.compile(
    r'\s*(?:read|open|view)\s+(?:the\s+)?(?P<workspace>(?:local\s+)?workspace\s+)?'
    r'(?:file\s+)?(?P<path>`[^`\n]+`|"[^"\n]+"|\'[^\'\n]+\'|[\w./~-]+)'
    r'(?P<suffix>\s+(?:from|in)\s+(?:the\s+)?(?:local\s+)?workspace)?', re.I,
)
_NEXT_LOCAL_FILE = re.compile(r'\s*(?:[;,]\s*|\s+(?:and|then)\s+)', re.I)
_LOCAL_FILE_END = re.compile(r'\s*(?:and\s+(?:explain what you found|summarize the contents))?[.!?]?\s*', re.I)
LOCAL_IMAGE_AND_HOST = re.compile(
    r'\s*(?:describe|check|inspect|review)\s+(?:(?:the|this|attached)\s+)*(?:image|screenshot)'
    r'\s+and\s+(?:check\s+)?(?:the\s+)?(?:local\s+)?host(?P<tools>\s+tools)?[.!?]?\s*', re.I,
)


def named_workspace_reads(instruction):
    """Recognize requests consisting of explicitly named local file reads."""
    paths, cursor = [], 0
    while match := _LOCAL_FILE.match(instruction, cursor):
        if not (match['workspace'] or match['suffix']):
            break
        raw = match['path']
        if raw[0] in ('`', '"', "'"):
            path = raw[1:-1]
        else:
            path = raw.removesuffix('.') if match.end() == len(instruction) else raw
        paths.append(path)
        if _LOCAL_FILE_END.fullmatch(instruction, match.end()):
            return paths
        separator = _NEXT_LOCAL_FILE.match(instruction, match.end())
        if not separator:
            break
        cursor = separator.end()
    return []


def local_completion_problem(expected_paths, observed_paths, image_request, image, hosts):
    if any(path not in observed_paths for path in expected_paths):
        return 'Each requested workspace file requires its own successful, complete read.'
    if image_request:
        if not image or not image.get('attachment_ids'):
            return 'A current attachment observation is required.'
        if image_request['tools']:
            if not any(isinstance(host['facts'].get('binaries'), dict) and host['facts']['binaries'] for host in hosts):
                return 'A local tool availability observation is required.'
        elif not hosts:
            return 'A successful local host diagnostic is required.'
    return ''


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
