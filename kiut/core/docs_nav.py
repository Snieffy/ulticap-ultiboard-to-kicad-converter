"""The documents' navigation: every document has a Contents section, every section ends with a link
back up to it, and every Contents (but the README's) starts with a link up to the README's Contents
-- one level up each time: section -> its document's Contents -> the README's Contents.

    navigate(text, up)     the document with its navigation (added or brought up to date)
    up_link(path, root)    the link up from a document's Contents to the README's Contents
    python -m kiut.core.docs_nav   brings every document in the KIUT folder up to date

Lines that are only an up link ("[↑ ...](...)") are the navigation's own; navigate() writes them
anew each time, so it can be run again. A Contents a document already has is kept (with the up
link added); one is made, from its sections' headings, where there is none.
"""
import re
from pathlib import Path
from typing import List, Optional

from kiut.core.docs_html import slug

UP_SECTION = '[↑ Contents](#contents)'
_NAV = re.compile(r'^\[↑ [^\]]*\]\([^)]*\)\s*$')
_HEAD = re.compile(r'^(#{1,2})\s+(.*?)\s*#*\s*$')
_FENCE = re.compile(r'^\s*(```|~~~)')


def up_link(path: Path, root: Path) -> Optional[str]:
    """The up link of a document's Contents: to the README Contents' part that lists it (none for
    the README itself)."""
    rel = path.resolve().relative_to(root.resolve())
    if rel == Path('README.md'):
        return None
    to_readme = '../' * (len(rel.parts) - 1) + 'README.md'
    part = 'file-formats' if rel.parts[:2] == ('docs', 'formats') else 'converting-and-viewing'
    return f'[↑ README contents]({to_readme}#{part})'


def _headings(lines: List[str], level: int = 2):
    """(line index, heading text, level) of every heading of level 1 and 2 outside code blocks --
    a section ends at the next of either (a document's parts are level 1) -- or of level 2 only."""
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            m = _HEAD.match(line)
            if m and (level == 1 or len(m.group(1)) == 2):
                yield i, m.group(2), len(m.group(1))


def _trim_end(block: List[str]) -> List[str]:
    while block and block[-1].strip() == '':
        block.pop()
    return block


def navigate(text: str, up: Optional[str]) -> str:
    lines = [ln for ln in text.split('\n') if not _NAV.match(ln)]
    sections = list(_headings(lines))
    if not sections:                                  # no sections: the up link at its end only
        body = _trim_end(lines)
        return '\n'.join(body + ([''] + [up] if up else [])) + '\n'
    if not any(h == 'Contents' for _, h, _ in sections):   # a Contents of its sections, before the first
        first = sections[0][0]
        made = ['## Contents', ''] + [f'- [{h}](#{slug(h)})' for _, h, _ in sections] + ['']
        lines = lines[:first] + made + lines[first:]
    heads = [h for h in _headings(lines, level=1) if h[0] >= next(_headings(lines))[0]]
    out: List[str] = []
    bounds = [i for i, _, _ in heads] + [len(lines)]
    out.extend(lines[:bounds[0]])
    for (start, title, level), end in zip(heads, bounds[1:]):
        block = lines[start:end]
        if level == 1:                                # a part's heading and its introduction
            out.extend(block)
            continue
        if title == 'Contents':
            rest = block[1:]
            while rest and rest[0].strip() == '':
                rest.pop(0)
            out.extend([block[0], ''] + ([up, ''] if up else []) + rest)
            continue
        block = _trim_end(block)
        tail = []
        if block and block[-1].strip() == '---':      # a rule before the next section stays last
            tail = ['', '---']
            block = _trim_end(block[:-1])
        out.extend(block + ['', UP_SECTION] + tail + [''])
    return '\n'.join(_trim_end(out)) + '\n'


def documents(root: Path) -> List[Path]:
    return [root / 'README.md'] + sorted((root / 'docs').rglob('*.md'))


if __name__ == '__main__':
    import sys
    root = Path(sys.argv[1]) if sys.argv[1:] else Path(__file__).resolve().parents[2]
    for p in documents(root):
        text = p.read_text(encoding='utf-8')
        new = navigate(text, up_link(p, root))
        if new != text:
            p.write_text(new, encoding='utf-8')
            print('updated', p.relative_to(root))
