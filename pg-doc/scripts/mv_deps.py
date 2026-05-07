"""Analyze base-table dependencies of one or more views/MVs and group by usage frequency."""
from __future__ import annotations

import argparse
import datetime as _dt
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence

from _common import ensure_dir, print_summary, render_md_table, write_md
from _db import connect, fetch_all

# Match identifiers that follow FROM / JOIN. Captures both quoted and unquoted.
_FROM_JOIN_RE = re.compile(
    r"""
    \b(?:FROM|JOIN)\s+
    (?:
        (?P<schema>[a-zA-Z_][\w]*) \. (?P<table>[a-zA-Z_][\w]*)   # schema-qualified
      | (?P<plain>[a-zA-Z_][\w]*)                                 # bare
      | "(?P<quoted>[^"]+)"                                        # quoted
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# CTE alias capture: WITH alias AS (...), alias2 AS (...), ...
# We collect anything `, name AS` after WITH, plus the first one.
_WITH_RE = re.compile(r"\bWITH\s+(?:RECURSIVE\s+)?(.+?)\s*SELECT\b", re.IGNORECASE | re.DOTALL)
_CTE_NAME_RE = re.compile(r"(?:^|,)\s*([a-zA-Z_][\w]*)\s+AS\s*\(", re.IGNORECASE)


def _collect_cte_names(viewdef: str) -> set[str]:
    """Best-effort scan for top-level CTE names so we can exclude them from base-table list."""
    names: set[str] = set()
    # Find any WITH ... SELECT pattern; allow nested but we only collect top-level CTEs
    # Simpler approach: scan the whole text for CTE patterns near WITH.
    for match in re.finditer(r"\bWITH\s+(?:RECURSIVE\s+)?", viewdef, re.IGNORECASE):
        # From the position of WITH onwards, scan CTE-like declarations until we hit a top-level SELECT
        tail = viewdef[match.end():]
        for cte in _CTE_NAME_RE.finditer(tail):
            names.add(cte.group(1))
            # Heuristic stop: when we encounter the closing of the CTE-list and hit a SELECT
            # This is imperfect for nested WITHs, but good enough for typical view defs.
        # Stop after first WITH block parsed
    return names


def extract_base_tables(viewdef: str) -> list[str]:
    """Return list of referenced base table names (de-duplicated, in encounter order)."""
    cte_names = _collect_cte_names(viewdef)
    seen: set[str] = set()
    out: list[str] = []
    for m in _FROM_JOIN_RE.finditer(viewdef):
        name = m.group("table") or m.group("plain") or m.group("quoted")
        if not name:
            continue
        if name in cte_names:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def fetch_viewdef(conn, schema: str, name: str) -> str | None:
    sql = """
        SELECT pg_get_viewdef(c.oid, true) AS def
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('v', 'm')
    """
    rows = fetch_all(conn, sql, (schema, name))
    if not rows:
        return None
    return rows[0]["def"]


def fetch_table_comments(conn, schema: str, names: Sequence[str]) -> dict[str, str]:
    if not names:
        return {}
    sql = """
        SELECT c.relname, obj_description(c.oid, 'pg_class') AS comment
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = ANY(%s)
    """
    rows = fetch_all(conn, sql, (schema, list(names)))
    return {r["relname"]: (r.get("comment") or "") for r in rows}


def render_md(
    views: list[tuple[str, list[str]]],
    comments: dict[str, str],
    filter_prefix: str | None,
) -> str:
    out = ["# View / MV Base-Table Dependency Analysis", ""]
    out.append(f"- Analyzed views: {len(views)}")
    out.append(f"- Filter prefix: `{filter_prefix or '(none)'}`")
    out.append("")

    # Per-view section
    out.append("## Per-view base tables")
    out.append("")
    out.append(render_md_table(
        ["View / MV", "Base tables used"],
        [[v, ", ".join(deps) if deps else "(none)"] for v, deps in views],
    ))
    out.append("")

    # Frequency analysis
    counter: dict[str, int] = defaultdict(int)
    used_in: dict[str, list[str]] = defaultdict(list)
    for view, deps in views:
        for d in deps:
            counter[d] += 1
            used_in[d].append(view)

    if not counter:
        out.append("## Frequency")
        out.append("")
        out.append("_No base tables matched._")
        return "\n".join(out)

    # Group by count desc
    by_count: dict[int, list[str]] = defaultdict(list)
    for tbl, cnt in counter.items():
        by_count[cnt].append(tbl)
    sorted_counts = sorted(by_count.keys(), reverse=True)

    out.append("## Frequency (descending)")
    out.append("")
    rows = []
    group_idx = 1
    for cnt in sorted_counts:
        tables = sorted(by_count[cnt])
        for t in tables:
            rows.append([
                f"#{group_idx}",
                t,
                comments.get(t, ""),
                ", ".join(sorted(used_in[t])),
                cnt,
            ])
        group_idx += 1
    out.append(render_md_table(
        ["Group", "Base table", "Comment", "Used by", "Count"],
        rows,
    ))
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    views_arg: list[str] = []
    for v in args.views or []:
        views_arg.extend(s.strip() for s in v.split(",") if s.strip())
    if not views_arg:
        raise SystemExit("--views is required (comma-separated, can repeat)")

    schema = args.schema or "public"
    output_dir = ensure_dir(Path(args.output).resolve() / "mv-deps")
    out_name = args.out_name or _dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".md"
    out_path = output_dir / out_name

    filter_prefix = args.filter_prefix
    results: list[tuple[str, list[str]]] = []
    all_base: set[str] = set()
    missing: list[str] = []

    with connect(mcp_name=args.mcp_name, conn=args.conn) as conn:
        for v in views_arg:
            vdef = fetch_viewdef(conn, schema, v)
            if vdef is None:
                missing.append(v)
                results.append((v, []))
                continue
            deps = extract_base_tables(vdef)
            if filter_prefix:
                deps = [d for d in deps if d.startswith(filter_prefix)]
            results.append((v, deps))
            all_base.update(deps)
        comments = fetch_table_comments(conn, schema, sorted(all_base))

    body = render_md(results, comments, filter_prefix)
    write_md(out_path, body)

    # Summary lines
    counter: dict[str, int] = defaultdict(int)
    for _v, deps in results:
        for d in deps:
            counter[d] += 1
    by_count: dict[int, list[str]] = defaultdict(list)
    for t, c in counter.items():
        by_count[c].append(t)
    summary_lines = []
    for c in sorted(by_count.keys(), reverse=True):
        summary_lines.append(f"{c}x: " + ", ".join(sorted(by_count[c])))
    if missing:
        summary_lines.append(f"missing views: {', '.join(missing)}")

    print_summary(
        title=f"pg-doc mv-deps: {len(views_arg)} views analyzed, {len(counter)} unique base tables",
        output_dir=out_path,
        extra_lines=summary_lines,
    )
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--views", action="append", default=[],
                        help="Comma-separated view/MV names; can repeat")
    parser.add_argument("--schema", default="public", help="Schema (default: public)")
    parser.add_argument("--filter-prefix", help="Only count base tables whose name starts with this prefix")
    parser.add_argument("--output", default="docs/pg", help="Base output dir (default: docs/pg)")
    parser.add_argument("--out-name", help="Output filename (default: timestamp.md)")
    parser.add_argument("--mcp-name")
    parser.add_argument("--conn")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="pg-doc mv-deps")
    add_arguments(ap)
    raise SystemExit(run(ap.parse_args()))
