"""One durable semantic plan: human-editable Markdown, artifact-bound approval.

Pipe-separated IDs are decoded only for existing pre-reset workflows and the
internal deterministic provider. They are never accepted as new relay plans.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_FIELDS = ("Scope", "Priority", "Priority reason", "Sources", "Topics", "Prerequisites", "Coverage")
_HEADER = re.compile(r"^## (l[1-9][0-9]*) — (.+)$", re.MULTILINE)
_FIELD_LABEL = r'(Scope|Priority|Priority reason|Sources|Topics|Prerequisites|Coverage)'
_FIELD_LINE = re.compile(r'^' + _FIELD_LABEL + r':\s*(.*)$')
# Model rendering of a plan sometimes carries a field label at a harmless
# small indent (observed: two leading spaces, inherited from the preceding
# bulleted Topics/Coverage list's continuation depth). Only a bounded
# indent is eligible, and only for the exact field the parser is next
# expecting for this block; any other indented text remains continuation.
_INDENTED_FIELD_LINE = re.compile(r'^ {1,3}' + _FIELD_LABEL + r':\s*(.*)$')


@dataclass(frozen=True)
class LectureSpec:
    lecture_id: str
    title: str
    fields: dict[str, str] = field(repr=False)
    markdown: str = field(repr=False)


def parse_plan(text: str) -> tuple[LectureSpec, ...]:
    if type(text) is not str or len(text.encode('utf-8')) > 256 * 1024:
        raise ValueError('invalid_lecture_plan')
    headers = list(_HEADER.finditer(text))
    if not 1 <= len(headers) <= 100 or text[:headers[0].start()].strip() not in ('', '# Lecture plan'):
        raise ValueError('invalid_lecture_plan')
    specs = []
    for index, header in enumerate(headers):
        block = text[header.start():headers[index + 1].start() if index + 1 < len(headers) else len(text)].strip()
        fields = {}
        current = None
        for line in block.splitlines()[1:]:
            match = _FIELD_LINE.match(line)
            if match is None:
                indented = _INDENTED_FIELD_LINE.match(line)
                # Structural location, not the label alone, licenses the
                # normalization: only the one field this block has not yet
                # collected may be recognized this way.
                if indented is not None and indented[1] == next((f for f in _FIELDS if f not in fields), None):
                    match = indented
            if match:
                current = match[1]
                if current in fields:
                    raise ValueError('duplicate_plan_field')
                fields[current] = match[2]
            elif line.strip():
                if current is None:
                    raise ValueError('invalid_lecture_plan')
                fields[current] += '\n' + line
        if set(fields) != set(_FIELDS) or any(not v.strip() for v in fields.values()):
            raise ValueError('incomplete_lecture_plan')
        if fields['Priority'] not in ('HIGH', 'MEDIUM', 'LOW PRIORITY / SKIM'):
            raise ValueError('invalid_plan_priority')
        if header[1] != f'l{index + 1}':
            raise ValueError('invalid_lecture_sequence')
        specs.append(LectureSpec(header[1], header[2], fields, block))
    return tuple(specs)


def lecture_ids_from_artifact(content: bytes) -> tuple[str, ...]:
    text = content.decode('utf-8')
    if text.startswith('l') and re.fullmatch(r'l[1-9][0-9]*(?:\|l[1-9][0-9]*)*', text):
        return tuple(text.split('|'))
    return tuple(spec.lecture_id for spec in parse_plan(text))
