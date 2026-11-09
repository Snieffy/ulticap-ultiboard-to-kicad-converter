# KIUT - KiCad Ulticap/Ultiboard import tools
# Copyright (C) 2026  Snieffy
# GNU General Public License version 3 or later: see LICENSE.
"""
kiut/core/docs_html.py -- KIUT's documents as web pages, made when they are read

Help in the KIUT window and in the viewer opens a document in the browser: README.md and every
.md under docs/ are rendered into a folder in the system's temporary folder (kiut_docs, made anew
each time, so the pages always match the installed documents) and the page asked for is opened.
Links between the documents lead to their pages, headings get GitHub's anchors, images and other
files are shown from the KIUT folder itself. (KIUT stage 10d, the author: no second set of documents
to keep, no connection needed.)

Only what KIUT's documents use is rendered -- headings, paragraphs and line breaks, lists (nested,
numbered), tables, fenced code, block quotes and GitHub's [!NOTE] blocks, rules, inline code,
bold, italic, links, images and <ins> -- KIUT's tests render every document and fail on anything
left over.

    open_document(APP_DIR, 'README.md')       # renders all, opens the README in the browser
"""
from __future__ import annotations

import html
import re
import shutil
import tempfile
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

FOLDER_NAME = 'kiut_docs'
ALLOWED_TAGS = ('ins', 'br')              # raw HTML the documents use, kept as HTML
CALLOUTS = {'NOTE': 'Note', 'TIP': 'Tip', 'IMPORTANT': 'Important', 'WARNING': 'Warning',
            'CAUTION': 'Caution'}

STYLE = """
:root { --fg:#1f2328; --bg:#ffffff; --muted:#59636e; --border:#d1d9e0; --code:#f6f8fa;
        --link:#0969da; --note:#0969da; --warn:#9a6700; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e6edf3; --bg:#0d1117; --muted:#9198a1; --border:#3d444d; --code:#151b23;
          --link:#4493f8; --note:#4493f8; --warn:#d29922; } }
body { margin:0; background:var(--bg); color:var(--fg);
       font:16px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif; }
header { border-bottom:1px solid var(--border); padding:8px 16px; color:var(--muted); font-size:14px; }
main { max-width:980px; margin:0 auto; padding:16px 16px 48px; }
a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
h1,h2 { border-bottom:1px solid var(--border); padding-bottom:.3em; }
h1,h2,h3,h4 { margin:1.4em 0 .6em; line-height:1.25; }
code { background:var(--code); padding:.15em .35em; border-radius:4px;
       font:85% ui-monospace,Consolas,"Liberation Mono",monospace; }
pre { background:var(--code); padding:12px 16px; border-radius:6px; overflow:auto; }
pre code { padding:0; background:none; font-size:85%; }
table { border-collapse:collapse; margin:0 0 16px; display:block; overflow:auto; }
th,td { border:1px solid var(--border); padding:6px 12px; vertical-align:top; }
th { background:var(--code); }
blockquote { margin:0 0 16px; padding:0 1em; color:var(--muted); border-left:.25em solid var(--border); }
.callout { border-left:.25em solid var(--note); padding:4px 1em; margin:0 0 16px; }
.callout.warning, .callout.caution { border-color:var(--warn); }
.callout .title { font-weight:600; color:var(--note); margin:8px 0; }
.callout.warning .title, .callout.caution .title { color:var(--warn); }
img { max-width:100%; }
hr { border:0; border-top:1px solid var(--border); margin:24px 0; }
footer { color:var(--muted); font-size:13px; margin-top:40px; }
"""


# ── anchors ──────────────────────────────────────────────────────────────────

def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, markup and punctuation dropped, spaces to '-'."""
    h = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', heading.strip())
    h = h.replace('`', '').lower()
    h = re.sub(r'[^\w\- ]', '', h)
    return h.replace(' ', '-')


# ── inline ───────────────────────────────────────────────────────────────────

class _Inline:
    """One document's inline rendering: code, links and images first, as
    placeholders, so nothing inside them is taken for markup."""

    def __init__(self, link) -> None:
        self.link = link                    # (target, is_image) -> the URL to write
        self.parts: List[str] = []

    def _hold(self, s: str) -> str:
        self.parts.append(s)
        return f'\x00{len(self.parts) - 1}\x00'

    def render(self, text: str) -> str:
        s = re.sub(r'\\([\\`*_{}\[\]()#+\-.!|<>~])', lambda m: self._hold(html.escape(m.group(1))), text)
        s = re.sub(r'(`+)(.+?)\1', lambda m: self._hold(f'<code>{html.escape(m.group(2).strip())}</code>'), s)
        s = re.sub(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)',
                   lambda m: self._hold(f'<img src="{html.escape(self.link(m.group(2), True), quote=True)}" '
                                        f'alt="{html.escape(self._plain(m.group(1)), quote=True)}">'), s)
        s = re.sub(r'\[((?:[^\[\]\x00]|\x00\d+\x00|\[[^\]]*\])*)\]\(([^)\s]+)\)',
                   lambda m: self._hold(f'<a href="{html.escape(self.link(m.group(2), False), quote=True)}">'
                                        f'{self._formatting(m.group(1))}</a>'), s)
        s = re.sub(r'(?<![("\w/])(https?://[^\s<>()]+[^\s<>().,;:])',
                   lambda m: self._hold(f'<a href="{html.escape(m.group(1), quote=True)}">'
                                        f'{html.escape(m.group(1))}</a>'), s)
        return self._restore(self._formatting(s))

    def _formatting(self, s: str) -> str:
        s = html.escape(s, quote=False)
        for tag in ALLOWED_TAGS:
            s = re.sub(rf'&lt;(/?){tag}\s*/?&gt;', rf'<\1{tag}>', s)
        s = re.sub(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', r'<strong>\1</strong>', s, flags=re.S)
        s = re.sub(r'(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])', r'<em>\1</em>', s, flags=re.S)
        s = re.sub(r'(?<![\w])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w])', r'<em>\1</em>', s)
        return s

    def _plain(self, s: str) -> str:
        return re.sub(r'\x00(\d+)\x00', lambda m: re.sub(r'<[^>]+>', '', self.parts[int(m.group(1))]), s)

    def _restore(self, s: str) -> str:
        while '\x00' in s:
            s = re.sub(r'\x00(\d+)\x00', lambda m: self.parts[int(m.group(1))], s)
        return s


# ── blocks ───────────────────────────────────────────────────────────────────

_LIST = re.compile(r'^( *)([-*+]|\d+[.)]) +(.*)$')
_TABLE_SEP = re.compile(r'^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')
_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_RULE = re.compile(r'^ {0,3}(-{3,}|\*{3,}|_{3,})\s*$')


class _Blocks:
    def __init__(self, inline: _Inline) -> None:
        self.inline = inline
        self.ids: Dict[str, int] = {}

    def heading_id(self, text: str) -> str:
        s = slug(text)
        n = self.ids.get(s, 0)
        self.ids[s] = n + 1
        return s if n == 0 else f'{s}-{n}'

    def _starts_block(self, line: str, next_line: str = '') -> bool:
        return bool(line.startswith('```') or _HEADING.match(line) or line.startswith('>')
                    or _LIST.match(line) or (line.startswith('|') and _TABLE_SEP.match(next_line or '')))

    def render(self, lines: List[str]) -> str:
        out: List[str] = []
        i, n = 0, len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            if line.lstrip().startswith('```'):
                fence_indent = len(line) - len(line.lstrip())
                lang = line.strip()[3:].strip()
                body = []
                i += 1
                while i < n and not lines[i].strip().startswith('```'):
                    body.append(lines[i][fence_indent:] if lines[i][:fence_indent].strip() == '' else lines[i])
                    i += 1
                i += 1
                cls = f' class="language-{html.escape(lang)}"' if lang else ''
                out.append(f'<pre><code{cls}>{html.escape(chr(10).join(body))}</code></pre>')
                continue
            m = _HEADING.match(line)
            if m:
                level, text = len(m.group(1)), m.group(2)
                out.append(f'<h{level} id="{self.heading_id(text)}">{self.inline.render(text)}</h{level}>')
                i += 1
                continue
            if _RULE.match(line):
                out.append('<hr>')
                i += 1
                continue
            if line.startswith('>'):
                quoted = []
                # GitHub's lazy continuation: a paragraph's next lines belong to the quote
                # without a '>' of their own (the README's legal notice)
                while i < n and (lines[i].startswith('>') or (
                        lines[i].strip() and quoted and quoted[-1].strip()
                        and not self._starts_block(lines[i], lines[i + 1] if i + 1 < n else ''))):
                    quoted.append(re.sub(r'^> ?', '', lines[i]))
                    i += 1
                out.append(self._quote(quoted))
                continue
            if line.startswith('|') and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
                rows = [line]
                i += 2
                while i < n and lines[i].startswith('|'):
                    rows.append(lines[i])
                    i += 1
                out.append(self._table(rows))
                continue
            if _LIST.match(line):
                block, i = self._take_list(lines, i)
                out.append(self._list(block))
                continue
            para = [line]
            i += 1
            while i < n and lines[i].strip() and not self._starts_block(lines[i], lines[i + 1] if i + 1 < n else ''):
                para.append(lines[i])
                i += 1
            out.append(f'<p>{self._para(para)}</p>')
        return '\n'.join(out)

    def _para(self, lines: List[str]) -> str:
        """A paragraph's lines rendered as one text (bold and code may run
        over a line end); a line ending in a backslash or two spaces: a break."""
        parts = []
        for j, ln in enumerate(lines):
            last = j == len(lines) - 1
            brk = (ln.endswith('\\') or ln.endswith('  ')) and not last
            text = ln.rstrip()
            if text.endswith('\\') and not text.endswith('\\\\'):
                text = text[:-1]
            parts.append(text.strip() + ('\x01' if brk else ''))
        return self.inline.render('\n'.join(parts)).replace('\x01', '<br>')

    def _quote(self, lines: List[str]) -> str:
        m = re.match(r'^\[!(\w+)\]\s*$', lines[0].strip()) if lines else None
        if m and m.group(1).upper() in CALLOUTS:
            kind = m.group(1).upper()
            return (f'<div class="callout {kind.lower()}"><p class="title">{CALLOUTS[kind]}</p>'
                    f'{self.render(lines[1:])}</div>')
        return f'<blockquote>{self.render(lines)}</blockquote>'

    @staticmethod
    def _cells(row: str) -> List[str]:
        row = row.strip()
        if row.startswith('|'):
            row = row[1:]
        if row.endswith('|') and not row.endswith('\\|'):
            row = row[:-1]
        cells, cur, j = [], '', 0
        while j < len(row):
            if row[j] == '\\' and j + 1 < len(row) and row[j + 1] == '|':
                cur += '\\|'
                j += 2
                continue
            if row[j] == '|':
                cells.append(cur)
                cur = ''
            else:
                cur += row[j]
            j += 1
        cells.append(cur)
        return [c.strip() for c in cells]

    def _table(self, rows: List[str]) -> str:
        head = ''.join(f'<th>{self.inline.render(c)}</th>' for c in self._cells(rows[0]))
        body = ''.join('<tr>' + ''.join(f'<td>{self.inline.render(c)}</td>' for c in self._cells(r)) + '</tr>'
                       for r in rows[1:])
        return f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'

    def _take_list(self, lines: List[str], i: int):
        """The list starting at line i: its lines, and the line after it."""
        indent = len(_LIST.match(lines[i]).group(1))
        block = []
        n = len(lines)
        while i < n:
            ln = lines[i]
            if not ln.strip():
                # a blank line: the list goes on when the next line is indented or another item
                nxt = lines[i + 1] if i + 1 < n else ''
                m = _LIST.match(nxt)
                if nxt.startswith(' ' * (indent + 2)) or (m and len(m.group(1)) == indent):
                    block.append(ln)
                    i += 1
                    continue
                break
            m = _LIST.match(ln)
            lead = len(ln) - len(ln.lstrip())
            if m and lead < indent:
                break
            if not m and lead <= indent and block and not block[-1].strip():
                break
            if not m and lead <= indent and self._starts_block(ln, lines[i + 1] if i + 1 < n else ''):
                break
            block.append(ln)
            i += 1
        return block, i

    def _list(self, block: List[str]) -> str:
        first = _LIST.match(block[0])
        indent = len(first.group(1))
        ordered = first.group(2)[0].isdigit()
        items: List[List[str]] = []
        for ln in block:
            m = _LIST.match(ln)
            if m and len(m.group(1)) == indent:
                content_col = len(m.group(1)) + len(m.group(2)) + 1
                items.append([m.group(3)])
                items[-1].append(content_col)          # type: ignore[arg-type]
            else:
                items[-1].insert(-1, ln)
        out = []
        for item in items:
            col = item.pop()
            body = [item[0]] + [ln[col:] if ln[:col].strip() == '' else ln.lstrip() for ln in item[1:]]
            html_body = self.render(body)
            if html_body.startswith('<p>') and html_body.count('<p>') == 1 and '\n<' not in html_body[3:]:
                html_body = html_body[3:-4]
            elif html_body.startswith('<p>') and html_body.count('<p>') == 1:
                end = html_body.index('</p>')
                html_body = html_body[3:end] + html_body[end + 4:]
            out.append(f'<li>{html_body}</li>')
        tag = 'ol' if ordered else 'ul'
        start = int(re.match(r'\d+', first.group(2)).group(0)) if ordered else 1
        attr = f' start="{start}"' if ordered and start != 1 else ''
        return f'<{tag}{attr}>' + ''.join(out) + f'</{tag}>'


# ── the documents ────────────────────────────────────────────────────────────

def documents(app_dir) -> List[Path]:
    """KIUT's public documents: README.md and every .md under docs/."""
    app_dir = Path(app_dir)
    return [app_dir / 'README.md'] + sorted((app_dir / 'docs').rglob('*.md'))


def render_document(md: Path, app_dir: Path, out_dir: Path, pages: Dict[Path, Path]) -> str:
    """One document's page (HTML text); pages: every document -> its page."""
    page = pages[md]

    def link(target: str, image: bool) -> str:
        if re.match(r'^[a-z][a-z0-9+.-]*:', target, re.I):
            return target
        path, _, frag = target.partition('#')
        if not path:
            return f'#{frag}'
        dest = (md.parent / path).resolve()
        if not image and dest in pages:
            rel = _relative(pages[dest], page)
            return rel + (f'#{frag}' if frag else '')
        return dest.as_uri() + (f'#{frag}' if frag else '') if dest.exists() else target

    text = md.read_text(encoding='utf-8')
    body = _Blocks(_Inline(link)).render(text.replace('\r\n', '\n').split('\n'))
    m = re.search(r'^#\s+(.*)$', text, re.M)
    title = html.escape(re.sub(r'[`*]', '', m.group(1)) if m else md.stem)
    readme = _relative(pages[(app_dir / 'README.md').resolve()], page) if (app_dir / 'README.md').resolve() in pages else ''
    from kiut import VERSION
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title}</title><style>{STYLE}</style></head>\n<body>'
            f'<header>KIUT {VERSION} — documentation · <a href="{readme}">README</a></header>\n'
            f'<main>\n{body}\n<footer>Made from {html.escape(str(md.relative_to(app_dir)))} in the KIUT '
            f'folder.</footer></main></body></html>\n')


def _relative(target: Path, page: Path) -> str:
    """target's URL from page's folder (both in the output folder)."""
    import os
    return os.path.relpath(target, page.parent).replace('\\', '/')


def render_all(app_dir, out_dir: Optional[Path] = None) -> Dict[Path, Path]:
    """Every document rendered into out_dir (default: the temporary folder's
    kiut_docs, emptied first), at its own place under it (README.html,
    docs/BOARDS.html, ...). Returns document -> page."""
    app_dir = Path(app_dir).resolve()
    out_dir = Path(out_dir) if out_dir is not None else Path(tempfile.gettempdir()) / FOLDER_NAME
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    pages = {md.resolve(): out_dir / md.resolve().relative_to(app_dir).with_suffix('.html')
             for md in documents(app_dir)}
    for md, page in pages.items():
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(render_document(md, app_dir, out_dir, pages), encoding='utf-8')
    return pages


def open_document(app_dir, name: str, opener=None) -> Path:
    """Every document rendered (render_all), the page of name (a path under
    the KIUT folder, e.g. 'README.md', 'docs/VIEWER.md') opened in the
    browser. Returns the page."""
    app_dir = Path(app_dir).resolve()
    pages = render_all(app_dir)
    page = pages[(app_dir / name).resolve()]
    (opener or webbrowser.open)(page.as_uri())
    return page
