"""Recursive view/MV lineage tree with Mermaid graph output."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from _common import ensure_dir, print_summary, sanitize_filename, write_md
from _db import connect, fetch_all
from mv_deps import extract_base_tables

RELKIND_LABELS = {
    "r": "table", "v": "view", "m": "matview", "f": "foreign",
    "p": "partitioned", "i": "index", "S": "sequence",
}
VIEW_KINDS = ("v", "m")


def fetch_relkind(conn, schema: str, names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    sql = """
        SELECT c.relname, c.relkind
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = ANY(%s)
    """
    rows = fetch_all(conn, sql, (schema, names))
    return {r["relname"]: r["relkind"] for r in rows}


def fetch_viewdef_batch(conn, schema: str, names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    sql = """
        SELECT c.relname, pg_get_viewdef(c.oid, true) AS def
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = ANY(%s) AND c.relkind IN ('v','m')
    """
    rows = fetch_all(conn, sql, (schema, names))
    return {r["relname"]: r["def"] for r in rows}


def fetch_table_comments(conn, schema: str, names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    sql = """
        SELECT c.relname, obj_description(c.oid, 'pg_class') AS comment
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = ANY(%s)
    """
    rows = fetch_all(conn, sql, (schema, names))
    return {r["relname"]: (r.get("comment") or "") for r in rows}


def build_tree(
    conn, schema: str, root: str, max_depth: int
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, str]]:
    """DFS expansion.

    Returns:
        edges: {parent: [child, ...]}
        relkinds: {name: relkind}
        comments: {name: comment}
    """
    edges: dict[str, list[str]] = {}
    relkinds: dict[str, str] = {}
    comments: dict[str, str] = {}
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(root, 0)]

    while queue:
        node, depth = queue.pop(0)
        if node in visited:
            continue
        visited.add(node)

        # Resolve kind
        if node not in relkinds:
            kinds = fetch_relkind(conn, schema, [node])
            relkinds.update(kinds)

        kind = relkinds.get(node, "r")
        if kind not in VIEW_KINDS or depth >= max_depth:
            continue

        defn = fetch_viewdef_batch(conn, schema, [node]).get(node)
        if not defn:
            continue

        deps = extract_base_tables(defn)
        edges[node] = deps

        if deps:
            dep_kinds = fetch_relkind(conn, schema, deps)
            relkinds.update(dep_kinds)
            dep_comments = fetch_table_comments(conn, schema, deps)
            comments.update(dep_comments)

            for dep in deps:
                if dep not in visited:
                    queue.append((dep, depth + 1))

    # Comments for all nodes
    all_nodes = list(visited)
    all_comments = fetch_table_comments(conn, schema, all_nodes)
    comments.update(all_comments)

    return edges, relkinds, comments


def _safe_id(name: str) -> str:
    """Make a Mermaid-safe node ID (letters/numbers/underscore)."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def render_mermaid(root: str, edges: dict[str, list[str]], relkinds: dict[str, str]) -> str:
    lines = ["```mermaid", "graph TD"]
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()

    def kind_shape(name: str) -> str:
        k = relkinds.get(name, "r")
        nid = _safe_id(name)
        label = name.replace('"', "'")
        if k in VIEW_KINDS:
            return f'{nid}["{label}"]'   # rectangle = view/MV
        return f'{nid}(("{label}"))'     # double circle = table

    all_nodes: set[str] = {root}
    for parent, children in edges.items():
        all_nodes.add(parent)
        all_nodes.update(children)

    for node in sorted(all_nodes):
        if node not in seen_nodes:
            lines.append(f"    {kind_shape(node)}")
            seen_nodes.add(node)

    for parent, children in edges.items():
        pid = _safe_id(parent)
        for child in children:
            cid = _safe_id(child)
            if (pid, cid) not in seen_edges:
                lines.append(f"    {pid} --> {cid}")
                seen_edges.add((pid, cid))

    lines.append("```")
    return "\n".join(lines)


def render_tree_text(
    root: str, edges: dict[str, list[str]], relkinds: dict[str, str], comments: dict[str, str],
    indent: int = 0, visited: set[str] | None = None
) -> list[str]:
    if visited is None:
        visited = set()
    kind = RELKIND_LABELS.get(relkinds.get(root, "r"), "?")
    comment = comments.get(root, "")
    prefix = "  " * indent
    marker = "▶" if indent == 0 else "└─"
    label = f"{prefix}{marker} `{root}` [{kind}]"
    if comment:
        label += f" — {comment}"
    lines = [label]
    if root in visited:
        lines[-1] += " *(cycle)*"
        return lines
    visited.add(root)
    for child in edges.get(root, []):
        lines.extend(render_tree_text(child, edges, relkinds, comments, indent + 1, visited))
    return lines


def render_md(
    root: str, edges: dict[str, list[str]], relkinds: dict[str, str], comments: dict[str, str]
) -> str:
    all_tables = {
        n for n, k in relkinds.items() if k not in VIEW_KINDS
    }
    out = [
        f"# Lineage: `{root}`",
        "",
        f"- Root: `{root}` ({RELKIND_LABELS.get(relkinds.get(root, 'r'), '?')})",
        f"- Nodes: {len(relkinds)}",
        f"- Base tables: {len(all_tables)}",
        "",
        "## Dependency Graph (Mermaid)",
        "",
        render_mermaid(root, edges, relkinds),
        "",
        "## Dependency Tree",
        "",
    ]
    out.extend(render_tree_text(root, edges, relkinds, comments))
    out.append("")
    out.append("## Base Tables")
    out.append("")
    bt_rows = [
        [t, comments.get(t, "")]
        for t in sorted(all_tables)
    ]
    from _common import render_md_table
    out.append(render_md_table(["Table", "Comment"], bt_rows))
    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    if not args.view:
        raise SystemExit("--view is required")
    schema = args.schema or "public"
    output_dir = ensure_dir(Path(args.output).resolve() / "lineage")
    out_name = sanitize_filename(args.view) + ".md"
    out_path = output_dir / out_name

    with connect(mcp_name=args.mcp_name, conn=args.conn) as conn:
        edges, relkinds, comments = build_tree(conn, schema, args.view, args.max_depth)

    body = render_md(args.view, edges, relkinds, comments)
    write_md(out_path, body)

    all_tables = [n for n, k in relkinds.items() if k not in VIEW_KINDS]
    all_views = [n for n, k in relkinds.items() if k in VIEW_KINDS]
    print_summary(
        title=f"pg-doc lineage: `{args.view}` → {len(relkinds)} nodes ({len(all_views)} views, {len(all_tables)} base tables)",
        output_dir=out_path,
        extra_lines=[f"base tables: {', '.join(sorted(all_tables))}"] if all_tables else None,
    )
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--view", required=True, help="Starting view/MV name")
    parser.add_argument("--schema", default="public", help="Schema (default: public)")
    parser.add_argument("--max-depth", type=int, default=10,
                        help="Max recursion depth (default: 10)")
    parser.add_argument("--output", default="docs/pg", help="Base output dir")
    parser.add_argument("--mcp-name")
    parser.add_argument("--conn")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="pg-doc lineage")
    add_arguments(ap)
    raise SystemExit(run(ap.parse_args()))
