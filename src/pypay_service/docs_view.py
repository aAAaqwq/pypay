from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import markdown

MAX_DOC_BYTES = 512 * 1024
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_HREF_RE = re.compile(r'href="([^"]+)"')
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MARKUP_RE = re.compile(r"[`*_>#]")


@dataclass(frozen=True)
class DocEntry:
    slug: str
    title: str
    excerpt: str
    path: Path


def _plain_title(text: str) -> str:
    match = _TITLE_RE.search(text)
    if match is None:
        return ""
    return _MARKUP_RE.sub("", match.group(1)).strip()


def _excerpt(text: str) -> str:
    cleaned = _CODE_FENCE_RE.sub("", text)
    cleaned = _HEADING_RE.sub("", cleaned)
    cleaned = _LINK_RE.sub(r"\1", cleaned)
    for line in cleaned.splitlines():
        line = _MARKUP_RE.sub("", line).strip()
        if line:
            return line[:140]
    return ""


def discover_docs(docs_dir: Path | None) -> list[DocEntry]:
    if docs_dir is None:
        return []
    try:
        root = docs_dir.expanduser().resolve()
    except OSError:
        return []
    if not root.is_dir():
        return []
    entries: list[DocEntry] = []
    for path in root.rglob("*.md"):
        relative = path.relative_to(root)
        if any(part.startswith((".", "_")) for part in relative.parts):
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
            if not resolved.is_file() or resolved.stat().st_size > MAX_DOC_BYTES:
                continue
            text = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        entries.append(
            DocEntry(
                slug=relative.with_suffix("").as_posix(),
                title=_plain_title(text) or relative.stem,
                excerpt=_excerpt(text),
                path=resolved,
            )
        )
    entries.sort(key=lambda entry: (entry.slug != "README", entry.slug))
    return entries


def find_doc(entries: list[DocEntry], slug: str) -> DocEntry | None:
    return next((entry for entry in entries if entry.slug == slug), None)


def render_markdown(text: str, *, slug: str, entries: list[DocEntry]) -> str:
    rendered = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
        output_format="html5",
    )
    slugs = {entry.slug for entry in entries}
    base = posixpath.dirname(slug)

    def rewrite(match: re.Match[str]) -> str:
        href = match.group(1)
        if href.startswith(("http://", "https://", "mailto:", "#", "/")):
            return match.group(0)
        target, separator, anchor = href.partition("#")
        if not target.endswith(".md"):
            return match.group(0)
        candidate = posixpath.normpath(posixpath.join(base, target))[:-3]
        if candidate not in slugs:
            return match.group(0)
        return f'href="/admin/docs/{quote(candidate, safe="/")}{separator}{anchor}"'

    return _HREF_RE.sub(rewrite, rendered)
