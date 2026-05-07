"""PostgreSQL connection management for pg-doc.

Resolution order:
  1. --conn / DATABASE_URL env
  2. .mcp.json scanned from CWD upward
  3. ~/.claude.json projects[*].mcpServers
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

PG_SERVER_PATTERNS = (
    "server-postgres",
    "postgres-mcp",
    "mcp-server-postgres",
)
PG_NAME_HINTS = ("pg", "postgres", "datalake", "data-lake", "data_lake")


def _from_env() -> Optional[str]:
    return os.environ.get("DATABASE_URL")


def _looks_like_pg_url(s: str) -> bool:
    return isinstance(s, str) and re.match(r"^postgres(ql)?://", s) is not None


def _extract_pg_url_from_server(server_name: str, cfg: dict) -> Optional[str]:
    """Given an MCP server config dict, try to find a PG URL.

    Strategies:
      - any string in `args` that matches postgres(ql)://...
      - a connection string built from env (PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT)
    """
    args = cfg.get("args") or []
    cmd = (cfg.get("command") or "").lower()

    is_pg_server = any(p in str(a).lower() for a in args for p in PG_SERVER_PATTERNS) \
        or any(p in cmd for p in PG_SERVER_PATTERNS)
    name_hints_match = any(h in server_name.lower() for h in PG_NAME_HINTS)

    # Look for explicit connection URL in args
    for a in args:
        if _looks_like_pg_url(str(a)):
            return str(a)

    # Look in env
    env = cfg.get("env") or {}
    if "DATABASE_URL" in env and _looks_like_pg_url(env["DATABASE_URL"]):
        return env["DATABASE_URL"]
    if all(k in env for k in ("PGHOST", "PGUSER", "PGDATABASE")):
        host = env["PGHOST"]
        port = env.get("PGPORT", "5432")
        user = env["PGUSER"]
        pwd = env.get("PGPASSWORD", "")
        db = env["PGDATABASE"]
        userinfo = f"{user}:{pwd}" if pwd else user
        return f"postgresql://{userinfo}@{host}:{port}/{db}"

    # Heuristic fallback: only return None if neither name nor server pattern matched
    if not (is_pg_server or name_hints_match):
        return None
    return None


def _from_local_mcp_json(start_dir: Path, target_name: Optional[str]) -> Optional[str]:
    """Walk up from start_dir looking for .mcp.json with PG-capable server."""
    current = start_dir.resolve()
    seen = set()
    while True:
        if current in seen:
            break
        seen.add(current)

        candidate = current / ".mcp.json"
        if candidate.is_file():
            try:
                cfg = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                cfg = None
            if cfg:
                url = _scan_mcp_servers(cfg.get("mcpServers", {}), target_name)
                if url:
                    return url

        if current.parent == current:
            break
        current = current.parent
    return None


def _from_claude_json(target_name: Optional[str]) -> Optional[str]:
    path = Path.home() / ".claude.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    # Top-level mcpServers (rare but possible)
    url = _scan_mcp_servers(data.get("mcpServers", {}), target_name)
    if url:
        return url

    # projects[*].mcpServers
    for _proj_path, proj in (data.get("projects") or {}).items():
        if not isinstance(proj, dict):
            continue
        url = _scan_mcp_servers(proj.get("mcpServers", {}), target_name)
        if url:
            return url
    return None


def _scan_mcp_servers(servers: dict, target_name: Optional[str]) -> Optional[str]:
    if not isinstance(servers, dict):
        return None
    if target_name and target_name in servers:
        url = _extract_pg_url_from_server(target_name, servers[target_name])
        if url:
            return url
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        url = _extract_pg_url_from_server(name, cfg)
        if url:
            return url
    return None


def get_connection_string(mcp_name: Optional[str] = None, conn: Optional[str] = None) -> str:
    """Resolve a PG connection string.

    Args:
        mcp_name: optional MCP server name to prefer when scanning configs.
        conn: optional explicit connection string (highest priority).
    """
    if conn:
        return conn
    env_url = _from_env()
    if env_url:
        return env_url

    cwd_url = _from_local_mcp_json(Path.cwd(), mcp_name)
    if cwd_url:
        return cwd_url

    home_url = _from_claude_json(mcp_name)
    if home_url:
        return home_url

    raise RuntimeError(
        "No PostgreSQL connection found.\n"
        "Set DATABASE_URL, or ensure a .mcp.json with a Postgres MCP server "
        "exists in this project (or in ~/.claude.json projects)."
    )


def connect(mcp_name: Optional[str] = None, conn: Optional[str] = None):
    """Return a psycopg2 connection (autocommit=True for read-only safety)."""
    dsn = get_connection_string(mcp_name=mcp_name, conn=conn)
    c = psycopg2.connect(dsn)
    c.autocommit = True
    return c


def fetch_all(cur_or_conn, sql: str, params: tuple | None = None) -> list[dict]:
    """Run a SELECT and return list[dict]. Accepts a connection or a cursor."""
    if hasattr(cur_or_conn, "cursor"):
        with cur_or_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]
    cur_or_conn.execute(sql, params or ())
    return [dict(r) for r in cur_or_conn.fetchall()]


if __name__ == "__main__":
    # Diagnostic mode: print resolved connection (mask password)
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp-name")
    ap.add_argument("--conn")
    args = ap.parse_args()
    try:
        dsn = get_connection_string(args.mcp_name, args.conn)
        masked = re.sub(r"://([^:]+):([^@]+)@", r"://\1:****@", dsn)
        print(f"Resolved: {masked}")
        c = connect(args.mcp_name, args.conn)
        rows = fetch_all(c, "SELECT current_database() AS db, current_user AS usr, version() AS ver")
        print(json.dumps(rows[0], default=str, ensure_ascii=False))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
