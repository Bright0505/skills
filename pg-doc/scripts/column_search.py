"""Column search: find which tables contain a given column name."""
from __future__ import annotations

import argparse
from pathlib import Path

from _common import ensure_dir, print_summary, render_md_table, sanitize_filename, write_md
from _db import connect, fetch_all


def fetch_columns(conn, pattern: str, match_mode: str, schemas: list[str]) -> list[dict]:
    if match_mode == "exact":
        op, val = "=", pattern
    elif match_mode == "like":
        op, val = "LIKE", pattern
    else:  # regex (PostgreSQL ~* = case-insensitive regex)
        op, val = "~*", pattern

    sql = f"""
        SELECT c.table_schema,
               c.table_name,
               c.column_name,
               c.ordinal_position,
               c.data_type,
               c.character_maximum_length,
               c.numeric_precision,
               c.numeric_scale,
               c.is_nullable,
               c.column_default,
               pgd.description AS column_comment,
               obj_description(pc.oid, 'pg_class') AS table_comment
          FROM information_schema.columns c
          JOIN pg_class pc
               ON pc.relname = c.table_name
          JOIN pg_namespace pn
               ON pn.oid = pc.relnamespace AND pn.nspname = c.table_schema
     LEFT JOIN pg_attribute pa
               ON pa.attrelid = pc.oid AND pa.attname = c.column_name
     LEFT JOIN pg_description pgd
               ON pgd.objoid = pc.oid AND pgd.objsubid = pa.attnum
         WHERE c.table_schema = ANY(%s)
           AND c.column_name {op} %s
         ORDER BY c.table_schema, c.table_name, c.ordinal_position
    """
    return fetch_all(conn, sql, (schemas, val))


def render_md(pattern: str, match_mode: str, rows: list[dict]) -> str:
    # Group by table
    tables: dict[str, list[dict]] = {}
    for r in rows:
        key = f"{r['table_schema']}.{r['table_name']}"
        tables.setdefault(key, []).append(r)

    out = [
        f"# Column Search: `{pattern}`",
        "",
        f"- Pattern: `{pattern}` ({match_mode})",
        f"- Tables matched: **{len(tables)}**",
        f"- Columns matched: **{len(rows)}**",
        "",
    ]

    # Summary table (one row per table)
    summary_rows = []
    for key, cols in tables.items():
        tbl = cols[0]
        col_names = ", ".join(f"`{c['column_name']}`" for c in cols)
        summary_rows.append([
            tbl["table_name"],
            tbl.get("table_comment") or "",
            col_names,
        ])
    out.append("## Summary")
    out.append("")
    out.append(render_md_table(["Table", "Comment", "Matched column(s)"], summary_rows))
    out.append("")

    # Detail per table
    out.append("## Detail")
    out.append("")
    for key, cols in tables.items():
        tbl = cols[0]
        out.append(f"### {tbl['table_name']}")
        if tbl.get("table_comment"):
            out.append(f"> {tbl['table_comment']}")
        out.append("")

        def fmt_type(c: dict) -> str:
            dt = c["data_type"]
            if c.get("character_maximum_length"):
                return f"{dt}({c['character_maximum_length']})"
            if c.get("numeric_precision") is not None and c.get("numeric_scale") is not None:
                return f"{dt}({c['numeric_precision']},{c['numeric_scale']})"
            return dt

        col_rows = [
            [c["column_name"], fmt_type(c), "YES" if c["is_nullable"] == "YES" else "NO",
             c.get("column_default") or "", c.get("column_comment") or ""]
            for c in cols
        ]
        out.append(render_md_table(["欄位名稱", "資料型態", "可為空", "預設值", "說明"], col_rows))
        out.append("")

    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    if not args.column:
        raise SystemExit("--column is required")

    match_mode = "regex" if args.regex else ("like" if args.like else "exact")
    schemas = args.schema or ["public"]
    output_dir = ensure_dir(Path(args.output).resolve() / "column-search")
    out_name = sanitize_filename(f"{args.column}") + ".md"
    out_path = output_dir / out_name

    with connect(mcp_name=args.mcp_name, conn=args.conn) as conn:
        rows = fetch_columns(conn, args.column, match_mode, schemas)

    body = render_md(args.column, match_mode, rows)
    write_md(out_path, body)

    tables_found = list({f"{r['table_name']}" for r in rows})
    print_summary(
        title=f"pg-doc column-search: '{args.column}' found in {len(tables_found)} tables",
        output_dir=out_path,
        items=sorted(tables_found),
    )
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--column", required=True, help="Column name to search for")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--exact", dest="exact", action="store_true", default=True,
                   help="Exact match (default)")
    g.add_argument("--like", action="store_true", help="SQL LIKE pattern (use %% for wildcard)")
    g.add_argument("--regex", action="store_true", help="Case-insensitive regex (PostgreSQL ~*)")
    parser.add_argument("--schema", action="append", default=[], help="Schema (default: public)")
    parser.add_argument("--output", default="docs/pg", help="Base output dir")
    parser.add_argument("--mcp-name")
    parser.add_argument("--conn")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="pg-doc column-search")
    add_arguments(ap)
    raise SystemExit(run(ap.parse_args()))
