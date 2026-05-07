"""Table size and activity analysis: identify large and cold tables."""
from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path

from _common import ensure_dir, print_summary, render_md_table, write_md
from _db import connect, fetch_all


def fetch_table_stats(conn, schemas: list[str], prefix: str | None, top: int) -> list[dict]:
    prefix_cond = "AND c.relname LIKE %s || '%%'" if prefix else ""
    prefix_param = [prefix] if prefix else []

    sql = f"""
        SELECT n.nspname                                           AS schema,
               c.relname                                           AS table_name,
               obj_description(c.oid, 'pg_class')                 AS comment,
               c.relkind                                           AS relkind,
               pg_total_relation_size(c.oid)                       AS total_bytes,
               pg_relation_size(c.oid)                             AS table_bytes,
               pg_indexes_size(c.oid)                              AS index_bytes,
               s.n_live_tup                                        AS live_rows,
               s.n_dead_tup                                        AS dead_rows,
               s.last_seq_scan                                     AS last_seq_scan,
               s.last_idx_scan                                     AS last_idx_scan,
               GREATEST(s.last_seq_scan, s.last_idx_scan)         AS last_access,
               s.seq_scan                                          AS seq_scans,
               s.idx_scan                                          AS idx_scans
          FROM pg_class c
          JOIN pg_namespace n    ON n.oid = c.relnamespace
     LEFT JOIN pg_stat_user_tables s
               ON s.relid = c.oid
         WHERE c.relkind IN ('r', 'p')
           AND n.nspname = ANY(%s)
           {prefix_cond}
         ORDER BY total_bytes DESC NULLS LAST
         LIMIT %s
    """
    params: list = [schemas if isinstance(schemas, list) else [schemas]]
    if prefix:
        params.append(prefix)
    params.append(top)
    return fetch_all(conn, sql, tuple(params))


def _fmt_bytes(b: int | None) -> str:
    if b is None:
        return "-"
    for unit, thresh in [("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]:
        if b >= thresh:
            return f"{b / thresh:.1f} {unit}"
    return f"{b} B"


def _fmt_dt(dt: _dt.datetime | None) -> str:
    if dt is None:
        return "never"
    return dt.strftime("%Y-%m-%d")


def render_md(rows: list[dict], cold_days: int, top: int, prefix: str | None) -> str:
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=cold_days)

    out = [
        "# Table Size & Activity Report",
        "",
        f"- Generated: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- Cold threshold: no access in **{cold_days} days**",
    ]
    if prefix:
        out.append(f"- Prefix filter: `{prefix}`")
    out.append(f"- Top: {top} tables by size")
    out.append("")

    # Size table
    out.append("## Size Ranking")
    out.append("")

    def cold_flag(r: dict) -> str:
        last = r.get("last_access")
        if last is None:
            return "❄ never"
        # psycopg2 returns datetime with tzinfo for timestamptz
        last_cmp = last.replace(tzinfo=_dt.timezone.utc) if last.tzinfo is None else last
        return "❄ cold" if last_cmp < cutoff else ""

    size_rows = [
        [
            r["table_name"],
            r.get("comment") or "",
            _fmt_bytes(r.get("total_bytes")),
            _fmt_bytes(r.get("table_bytes")),
            _fmt_bytes(r.get("index_bytes")),
            f"{r.get('live_rows') or 0:,}",
            _fmt_dt(r.get("last_access")),
            cold_flag(r),
        ]
        for r in rows
    ]
    out.append(render_md_table(
        ["Table", "Comment", "Total", "Table", "Index", "Live rows", "Last access", "Status"],
        size_rows,
    ))
    out.append("")

    # Cold tables summary
    cold = [r for r in rows if r.get("last_access") is None or
            (r["last_access"].replace(tzinfo=_dt.timezone.utc)
             if r["last_access"] and r["last_access"].tzinfo is None
             else (r.get("last_access") or _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc))) < cutoff]
    if cold:
        out.append(f"## Cold Tables ({len(cold)})")
        out.append("")
        out.append(f"> Tables with no recorded access in the last {cold_days} days.")
        out.append("")
        out.append(render_md_table(
            ["Table", "Comment", "Total size", "Last access"],
            [[r["table_name"], r.get("comment") or "", _fmt_bytes(r.get("total_bytes")), _fmt_dt(r.get("last_access"))]
             for r in cold],
        ))

    return "\n".join(out)


def run(args: argparse.Namespace) -> int:
    schemas = args.schema or ["public"]
    output_dir = ensure_dir(Path(args.output).resolve() / "size")
    out_name = _dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".md"
    out_path = output_dir / out_name

    with connect(mcp_name=args.mcp_name, conn=args.conn) as conn:
        rows = fetch_table_stats(conn, schemas, args.prefix, args.top)

    body = render_md(rows, args.cold_days, args.top, args.prefix)
    write_md(out_path, body)

    total_size = sum(r.get("total_bytes") or 0 for r in rows)
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=args.cold_days)
    cold_count = sum(
        1 for r in rows
        if r.get("last_access") is None or
        (r["last_access"].replace(tzinfo=_dt.timezone.utc) if r["last_access"] and r["last_access"].tzinfo is None
         else (r.get("last_access") or _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc))) < cutoff
    )
    print_summary(
        title=f"pg-doc size: top {len(rows)} tables ({_fmt_bytes(total_size)} total)",
        output_dir=out_path,
        extra_lines=[
            f"cold tables (>{args.cold_days}d no access): {cold_count}",
        ],
        items=[f"{r['table_name']}  {_fmt_bytes(r.get('total_bytes'))}" for r in rows[:10]],
    )
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--top", type=int, default=30, help="Number of tables to show (default: 30)")
    parser.add_argument("--cold-days", type=int, default=90,
                        help="Days without access to consider a table cold (default: 90)")
    parser.add_argument("--prefix", help="Filter tables by name prefix")
    parser.add_argument("--schema", action="append", default=[], help="Schema (default: public)")
    parser.add_argument("--output", default="docs/pg", help="Base output dir")
    parser.add_argument("--mcp-name")
    parser.add_argument("--conn")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="pg-doc size")
    add_arguments(ap)
    raise SystemExit(run(ap.parse_args()))
