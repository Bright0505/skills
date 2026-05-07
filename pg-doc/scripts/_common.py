"""Shared helpers for pg-doc renderers."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, Sequence

INVALID_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename across macOS/Linux/Windows.

    Keeps Chinese / unicode characters; only strips OS-incompatible chars.
    Trims leading/trailing whitespace and dots.
    """
    if not name:
        return "_"
    cleaned = INVALID_FILENAME_CHARS.sub("_", name)
    cleaned = cleaned.strip(" .")
    return cleaned or "_"


def render_md_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render a github-flavored markdown table.

    Empty / None cells render as a blank string. Pipes inside cells are escaped.
    """
    def cell(v: object) -> str:
        if v is None:
            return ""
        s = str(v)
        # escape pipes; collapse newlines so tables stay valid
        return s.replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = []
    lines.append("| " + " | ".join(cell(h) for h in headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for r in rows:
        lines.append("| " + " | ".join(cell(c) for c in r) + " |")
    return "\n".join(lines)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_md(path: Path, body: str) -> None:
    ensure_dir(path.parent)
    if not body.endswith("\n"):
        body = body + "\n"
    path.write_text(body, encoding="utf-8")


def print_summary(
    title: str,
    output_dir: Path | str | None = None,
    items: Iterable[str] | None = None,
    extra_lines: Iterable[str] | None = None,
    max_items: int = 20,
) -> None:
    """Pretty-print a short completion summary to stdout for Claude to read.

    Format:
        ✓ <title>
          output: <output_dir>
          <extra_lines...>
          - item1
          - item2
          ... and N more
    """
    print(f"✓ {title}")
    if output_dir is not None:
        print(f"  output: {output_dir}")
    if extra_lines:
        for line in extra_lines:
            print(f"  {line}")
    if items is not None:
        items = list(items)
        for i, it in enumerate(items[:max_items]):
            print(f"  - {it}")
        if len(items) > max_items:
            print(f"  ... and {len(items) - max_items} more")
    sys.stdout.flush()
