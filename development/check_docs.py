#!/usr/bin/env python3
"""Check repository Markdown structure and local links using only the stdlib.

Run from anywhere with ``python3 development/check_docs.py``. External URLs
are intentionally ignored so the result is deterministic and offline-safe.
"""

from __future__ import annotations

import html
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]

# Add an entry only for a checked-in format that genuinely cannot contain one
# H1. Every exception must carry a non-empty, reviewable justification.
H1_EXEMPTIONS: dict[str, str] = {}

ATX_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$")
SETEXT_H1_RE = re.compile(r"^ {0,3}=+[ \t]*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
REFERENCE_RE = re.compile(
    r"^ {0,3}\[(?!\^)[^]\n]+\]:[ \t]*(?:<([^>]+)>|([^ \t]+))"
)
HTML_LINK_RE = re.compile(r"\b(?:href|src)\s*=\s*([\"'])(.*?)\1", re.I)
HTML_ID_RE = re.compile(r"\b(?:id|name)\s*=\s*([\"'])(.*?)\1", re.I)
HTML_H1_RE = re.compile(r"<h1(?:\s[^>]*)?>", re.I)
LINK_START_RE = re.compile(r"]\s*\(")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

Error = tuple[str, int, str]


@dataclass
class Document:
    headings: set[str]
    h1_lines: list[int]
    links: list[tuple[int, str]]


# Optional file of path prefixes to skip, one per line ("#" starts a comment).
# Use it only for a byte-for-byte mirror of documents whose relative links
# resolve in another repository's layout. This repository ships no such file.
IGNORE_FILE = ROOT / "development" / "check_docs.ignore"


def ignored_prefixes() -> tuple[str, ...]:
    """Return the path prefixes listed in IGNORE_FILE, or none."""
    if not IGNORE_FILE.is_file():
        return ()
    lines = IGNORE_FILE.read_text(encoding="utf-8").splitlines()
    return tuple(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))


def tracked_markdown() -> list[Path]:
    """List tracked and unignored worktree Markdown files in stable order."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", b"").decode("utf-8", "replace").strip()
        raise RuntimeError(stderr or str(exc)) from exc

    paths = (
        Path(raw.decode("utf-8", "surrogateescape"))
        for raw in result.stdout.split(b"\0")
        if raw
    )
    # `git ls-files` retains worktree deletions until commit. Skipping absent
    # sources lets the gate run during cleanup; inbound links still fail.
    skipped = ignored_prefixes()
    return sorted(
        (
            path
            for path in paths
            if path.suffix.lower() == ".md"
            and (ROOT / path).is_file()
            and not (skipped and path.as_posix().startswith(skipped))
        ),
        key=lambda path: path.as_posix(),
    )


def strip_comments(line: str, active: bool) -> tuple[str, bool]:
    """Mask HTML comments without changing character positions."""
    chars = list(line)
    cursor = 0
    while cursor < len(line):
        if active:
            end = line.find("-->", cursor)
            if end < 0:
                chars[cursor:] = " " * (len(chars) - cursor)
                return "".join(chars), True
            chars[cursor : end + 3] = " " * (end + 3 - cursor)
            cursor, active = end + 3, False
            continue
        start = line.find("<!--", cursor)
        if start < 0:
            break
        end = line.find("-->", start + 4)
        if end < 0:
            chars[start:] = " " * (len(chars) - start)
            return "".join(chars), True
        chars[start : end + 3] = " " * (end + 3 - start)
        cursor = end + 3
    return "".join(chars), active


def visible_markdown(path: Path, lines: list[str]) -> tuple[list[tuple[int, str]], list[Error]]:
    """Remove fenced blocks and comments, reporting an unclosed fence."""
    visible: list[tuple[int, str]] = []
    errors: list[Error] = []
    fence: tuple[str, int, int] | None = None  # character, length, line
    in_comment = False

    for number, raw in enumerate(lines, 1):
        if fence:
            match = FENCE_RE.match(raw)
            if match:
                marker, remainder = match.groups()
                if marker[0] == fence[0] and len(marker) >= fence[1] and not remainder.strip():
                    fence = None
            continue

        line, in_comment = strip_comments(raw, in_comment)
        match = FENCE_RE.match(line)
        if match:
            marker, info = match.groups()
            if marker[0] != "`" or "`" not in info:
                fence = marker[0], len(marker), number
                continue
        visible.append((number, line))

    if fence:
        errors.append((path.as_posix(), fence[2], f"unclosed {fence[0] * fence[1]} code fence"))
    return visible, errors


def mask_inline_code(line: str) -> str:
    chars = list(line)
    cursor = 0
    while cursor < len(line):
        if line[cursor] != "`" or (cursor and line[cursor - 1] == "\\"):
            cursor += 1
            continue
        end = cursor
        while end < len(line) and line[end] == "`":
            end += 1
        marker = line[cursor:end]
        close = line.find(marker, end)
        if close < 0:
            cursor = end
            continue
        chars[cursor : close + len(marker)] = " " * (close + len(marker) - cursor)
        cursor = close + len(marker)
    return "".join(chars)


def github_slug(raw: str) -> str:
    """Produce GitHub-style heading IDs, including repeated spaces."""
    text = re.sub(r"[ \t]+#+[ \t]*$", "", raw).strip()
    text = re.sub(r"!?\[([^]]*)]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^]]+)]\[[^]]*]", r"\1", text)
    text = re.sub(r"<[^>]+>|[*~`]", "", text)
    text = unicodedata.normalize("NFKC", html.unescape(text)).strip().lower()
    slug: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if character in {" ", "\t"}:
            slug.append("-")
        elif character in {"-", "_"} or category[0] in {"L", "N"} or category in {"Mn", "Mc"}:
            slug.append(character)
    return "".join(slug)


def extract_headings(visible: list[tuple[int, str]]) -> tuple[set[str], list[int]]:
    headings: set[str] = set()
    slug_counts: dict[str, int] = {}
    h1_lines: list[int] = []
    previous: tuple[int, str] | None = None

    def add(number: int, level: int, text: str) -> None:
        if level == 1:
            h1_lines.append(number)
        base = github_slug(text)
        count = slug_counts.get(base, 0)
        headings.add(base if count == 0 else f"{base}-{count}")
        slug_counts[base] = count + 1

    for number, line in visible:
        headings.update(html.unescape(match.group(2)) for match in HTML_ID_RE.finditer(line))
        h1_lines.extend(number for _ in HTML_H1_RE.finditer(line))
        match = ATX_RE.match(line)
        if match:
            marker, text = match.groups()
            add(number, len(marker), text)
            previous = None
        elif SETEXT_H1_RE.match(line) and previous and previous[1].strip():
            add(previous[0], 1, previous[1].strip())
            previous = None
        else:
            previous = (number, line) if line.strip() else None
    return headings, h1_lines


def inline_targets(line: str) -> list[str]:
    """Extract same-line Markdown link and image destinations."""
    targets: list[str] = []
    for start in LINK_START_RE.finditer(line):
        cursor = start.end()
        while cursor < len(line) and line[cursor] in " \t":
            cursor += 1
        if cursor >= len(line):
            continue
        if line[cursor] == "<":
            end = line.find(">", cursor + 1)
            if end >= 0:
                targets.append(line[cursor + 1 : end])
            continue

        begin, depth, escaped = cursor, 0, False
        while cursor < len(line):
            character = line[cursor]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    targets.append(line[begin:cursor])
                    break
                depth -= 1
            elif character in " \t" and depth == 0:
                targets.append(line[begin:cursor])
                break
            cursor += 1
    return targets


def extract_links(visible: list[tuple[int, str]]) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    for number, raw in visible:
        line = mask_inline_code(raw)
        reference = REFERENCE_RE.match(line)
        if reference:
            links.append((number, reference.group(1) or reference.group(2)))
        links.extend((number, target) for target in inline_targets(line))
        links.extend((number, match.group(2)) for match in HTML_LINK_RE.finditer(line))
    return links


def load(path: Path) -> tuple[Document | None, list[Error]]:
    raw = (ROOT / path).read_bytes()
    errors: list[Error] = []
    if raw and not raw.endswith(b"\n"):
        errors.append((path.as_posix(), len(raw.splitlines()), "missing final newline"))
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        line = raw[: exc.start].count(b"\n") + 1
        return None, errors + [(path.as_posix(), line, "file is not valid UTF-8")]

    visible, structure_errors = visible_markdown(path, lines)
    headings, h1_lines = extract_headings(visible)
    return Document(headings, h1_lines, extract_links(visible)), errors + structure_errors


def normalized_target(raw: str) -> str:
    target = html.unescape(raw.strip())
    return re.sub(r"\\([\\()`*{}\[\]<>#+.!_-])", r"\1", target)


def check_links(documents: dict[Path, Document]) -> list[Error]:
    errors: list[Error] = []
    root = ROOT.resolve()
    for source, document in documents.items():
        for number, raw in document.links:
            target = normalized_target(raw)
            if not target or target.startswith(("//", "/")) or SCHEME_RE.match(target):
                continue
            try:
                parsed = urlsplit(target)
            except ValueError:
                errors.append((source.as_posix(), number, f"invalid local link: {raw!r}"))
                continue
            relative = source.parent / unquote(parsed.path) if parsed.path else source
            try:
                destination = (ROOT / relative).resolve()
                destination.relative_to(root)
            except ValueError:
                errors.append((source.as_posix(), number, f"local link escapes repository: {raw!r}"))
                continue
            if not destination.exists():
                errors.append((source.as_posix(), number, f"broken local link: {raw!r}"))
                continue
            if not parsed.fragment:
                continue
            anchor_file = destination / "README.md" if destination.is_dir() else destination
            if anchor_file.suffix.lower() != ".md":
                continue
            try:
                anchor_path = anchor_file.relative_to(root)
            except ValueError:
                continue
            anchor_doc = documents.get(anchor_path)
            fragment = unquote(parsed.fragment).removeprefix("user-content-")
            if anchor_doc is not None and fragment not in anchor_doc.headings:
                errors.append(
                    (
                        source.as_posix(),
                        number,
                        f"unknown Markdown anchor {fragment!r} in {anchor_path.as_posix()}",
                    )
                )
    return errors


def main() -> int:
    try:
        paths = tracked_markdown()
    except RuntimeError as exc:
        print(f"Documentation check could not run: {exc}", file=sys.stderr)
        return 2

    documents: dict[Path, Document] = {}
    errors: list[Error] = []
    for path in paths:
        document, file_errors = load(path)
        errors.extend(file_errors)
        if document is None:
            continue
        documents[path] = document
        exemption = H1_EXEMPTIONS.get(path.as_posix())
        if exemption is not None and not exemption.strip():
            errors.append((path.as_posix(), 1, "H1 exemption has no justification"))
        elif exemption is None and len(document.h1_lines) != 1:
            line = document.h1_lines[1] if len(document.h1_lines) > 1 else 1
            errors.append(
                (path.as_posix(), line, f"expected exactly one H1, found {len(document.h1_lines)}")
            )

    tracked = {path.as_posix() for path in paths}
    for stale in sorted(set(H1_EXEMPTIONS) - tracked):
        errors.append((stale, 0, "stale H1 exemption for an untracked file"))
    errors.extend(check_links(documents))

    if errors:
        errors.sort()
        print(f"Documentation check failed with {len(errors)} problem(s):", file=sys.stderr)
        for path, line, message in errors:
            location = f"{path}:{line}" if line else path
            print(f"  {location}: {message}", file=sys.stderr)
        return 1
    print(f"Documentation check passed ({len(documents)} Markdown files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
