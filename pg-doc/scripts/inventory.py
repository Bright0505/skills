"""Inventory: list tables matching prefix(es) and emit per-table column markdown."""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Sequence

from _common import ensure_dir, print_summary, render_md_table, sanitize_filename, write_md
from _db import connect, fetch_all

# relkind: r=ordinary, v=view, m=materialized view, f=foreign, p=partitioned
RELKIND_DEFAULT = ("r", "v", "m", "f", "p")
RELKIND_LABEL = {
    "r": "table",
    "v": "view",
    "m": "matview",
    "f": "foreign",
    "p": "partitioned",
}


def fetch_tables(conn, prefixes: Sequence[str], schemas: Sequence[str], relkinds: Sequence[str]) -> list[dict]:
    sql = """
        SELECT n.nspname AS schema,
               c.relname AS table_name,
               c.relkind AS relkind,
               obj_description(c.oid, 'pg_class') AS comment
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind = ANY(%s)
           AND n.nspname = ANY(%s)
           AND (
               %s::text[] = '{}'::text[]
               OR EXISTS (
                   SELECT 1 FROM unnest(%s::text[]) p
                    WHERE c.relname LIKE p || '%%'
               )
           )
         ORDER BY c.relname
    """
    return fetch_all(
        conn,
        sql,
        (list(relkinds), list(schemas), list(prefixes), list(prefixes)),
    )


def fetch_columns(conn, schema: str, table_names: Sequence[str]) -> list[dict]:
    """Fetch column info for given tables. Uses pg_attribute for parity with partitioned tables."""
    sql = """
        SELECT n.nspname        AS schema,
               c.relname        AS table_name,
               a.attnum         AS ordinal_position,
               a.attname        AS column_name,
               format_type(a.atttypid, a.atttypmod) AS data_type,
               NOT a.attnotnull AS is_nullable,
               pg_get_expr(d.adbin, d.adrelid) AS column_default,
               col_description(c.oid, a.attnum) AS column_comment
          FROM pg_attribute a
          JOIN pg_class c     ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_attrdef d   ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE a.attnum > 0
           AND NOT a.attisdropped
           AND n.nspname = %s
           AND c.relname = ANY(%s)
         ORDER BY c.relname, a.attnum
    """
    return fetch_all(conn, sql, (schema, list(table_names)))


def filter_excluded(rows: list[dict], excludes: Sequence[str]) -> tuple[list[dict], list[dict]]:
    """Split rows into (kept, excluded) by suffix matches."""
    if not excludes:
        return rows, []
    excludes = [e for e in excludes if e]
    kept, dropped = [], []
    for r in rows:
        name = r["table_name"]
        if any(name.endswith(suf) for suf in excludes):
            dropped.append(r)
        else:
            kept.append(r)
    return kept, dropped


def render_inventory_md(rows: list[dict], dropped: list[dict], excludes: Sequence[str], prefixes: Sequence[str]) -> str:
    headers = ["Table", "Type", "Comment"]
    body_rows = [
        [r["table_name"], RELKIND_LABEL.get(r["relkind"], r["relkind"]), r.get("comment") or ""]
        for r in rows
    ]
    out = []
    out.append(f"# Table Inventory")
    out.append("")
    if prefixes:
        out.append(f"- Prefix(es): `{', '.join(prefixes)}`")
    if excludes:
        out.append(f"- Excluded suffixes: `{', '.join(excludes)}`")
    out.append(f"- Kept: **{len(rows)}** / Dropped: **{len(dropped)}**")
    out.append("")
    out.append(render_md_table(headers, body_rows))
    if dropped:
        out.append("")
        out.append(f"## Excluded ({len(dropped)})")
        out.append("")
        out.append(render_md_table(
            headers,
            [[r["table_name"], RELKIND_LABEL.get(r["relkind"], r["relkind"]), r.get("comment") or ""] for r in dropped],
        ))
    return "\n".join(out)


def render_table_md(table_row: dict, columns: list[dict]) -> tuple[str, str]:
    """Return (filename, body) for one table's column doc."""
    title = table_row.get("comment") or table_row["table_name"]
    filename = sanitize_filename(title) + ".md"

    headers = ["欄位名稱", "資料型態", "可為空", "預設值", "說明"]
    rows = []
    for c in columns:
        rows.append([
            c["column_name"],
            c["data_type"],
            "YES" if c["is_nullable"] else "NO",
            c.get("column_default") or "",
            c.get("column_comment") or "",
        ])

    body_lines = [
        f"# {title}",
        "",
        f"- Table: `{table_row['table_name']}`",
        f"- Type: {RELKIND_LABEL.get(table_row['relkind'], table_row['relkind'])}",
    ]
    if table_row.get("comment"):
        body_lines.append(f"- Comment: {table_row['comment']}")
    body_lines.append(f"- Columns: {len(columns)}")
    body_lines.append("")
    body_lines.append(render_md_table(headers, rows))
    return filename, "\n".join(body_lines)


def run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output).resolve()
    tables_dir = ensure_dir(output_dir / "tables")

    prefixes = [p for p in (args.prefix or []) if p]
    excludes = [s for s in (args.exclude or []) if s]
    schemas = args.schema or ["public"]
    relkinds = list(args.relkinds) if args.relkinds else list(RELKIND_DEFAULT)

    with connect(mcp_name=args.mcp_name, conn=args.conn) as conn:
        all_rows = fetch_tables(conn, prefixes, schemas, relkinds)
        kept, dropped = filter_excluded(all_rows, excludes)

        # Inventory file
        inv_body = render_inventory_md(kept, dropped, excludes, prefixes)
        inv_path = tables_dir / "_inventory.md"
        write_md(inv_path, inv_body)

        # Per-table docs (skip those without a sensible name)
        # Group by schema for column queries
        schema_to_tables: dict[str, list[dict]] = defaultdict(list)
        for r in kept:
            schema_to_tables[r["schema"]].append(r)

        produced: list[str] = []
        if not args.skip_columns:
            for schema, rows in schema_to_tables.items():
                names = [r["table_name"] for r in rows]
                cols = fetch_columns(conn, schema, names)
                cols_by_table: dict[str, list[dict]] = defaultdict(list)
                for c in cols:
                    cols_by_table[c["table_name"]].append(c)
                for r in rows:
                    fname, body = render_table_md(r, cols_by_table.get(r["table_name"], []))
                    write_md(tables_dir / fname, body)
                    produced.append(fname)

    print_summary(
        title=f"pg-doc inventory: {len(kept)} tables documented",
        output_dir=tables_dir,
        items=produced if produced else None,
        extra_lines=[
            f"prefix={','.join(prefixes) or '(none)'}",
            f"excluded suffixes={','.join(excludes) or '(none)'}",
            f"dropped={len(dropped)}",
            f"inventory: {inv_path.relative_to(output_dir.parent) if inv_path.is_relative_to(output_dir.parent) else inv_path}",
        ],
    )
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prefix", action="append", default=[], help="Table name prefix; can repeat")
    parser.add_argument("--exclude", action="append", default=[], help="Suffix to exclude (e.g. _cold); can repeat")
    parser.add_argument("--schema", action="append", default=[], help="Schema (default: public); can repeat")
    parser.add_argument("--relkinds", help="Comma-separated relkinds; default r,v,m,f,p",
                        type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    parser.add_argument("--skip-columns", action="store_true", help="Only emit _inventory.md, no per-table column docs")
    parser.add_argument("--output", default="docs/pg", help="Base output directory (default: docs/pg)")
    parser.add_argument("--mcp-name", help="Prefer this MCP server name when auto-detecting connection")
    parser.add_argument("--conn", help="Explicit DSN; overrides env / MCP detection")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="pg-doc inventory")
    add_arguments(ap)
    raise SystemExit(run(ap.parse_args()))
