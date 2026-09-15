"""Pure source-snippet Markdown grammar."""
import re
from dataclasses import dataclass, field

DIRECTIVE = re.compile(r'^\[\[SOURCE_SNIP source="([a-z0-9][a-z0-9._-]{0,63})" page=([1-9][0-9]{0,5}) purpose="([^"\n\x00-\x1f]{1,300})"\]\]$', re.MULTILINE)


@dataclass(frozen=True)
class SourceSnippet:
    source_id: str
    page: int
    purpose: str = field(repr=False)
    offset: int


def parse_snippets(text):
    snippets = []
    offset, fence = 0, None
    for line in text.splitlines(keepends=True):
        value = line.rstrip('\r\n')
        if value.lstrip().startswith(('```', '~~~')):
            marker = value.lstrip()[:3]
            fence = None if fence == marker else marker if fence is None else fence
        if 'SOURCE_SNIP' in value and fence is None:
            match = DIRECTIVE.fullmatch(value)
            if not match or match[1] in ('.', '..'):
                raise ValueError('invalid_source_snippet_directive')
            snippets.append(SourceSnippet(match[1], int(match[2]), match[3], offset))
        offset += len(line)
    if len(snippets) > 32:
        raise ValueError('too_many_source_snippets')
    return tuple(snippets)
