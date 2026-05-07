#!/usr/bin/env python3
"""pg-doc: PostgreSQL schema documentation & dependency analysis.

Subcommands:
  inventory     -- table inventory + per-table column markdown
  mv-deps       -- view/MV base-table dependency frequency analysis
  column-search -- find which tables contain a given column (TODO)
  size          -- table size + activity (cold/hot) report (TODO)
  lineage       -- recursive view/MV lineage tree (TODO)

Connection resolution (in order):
  1. --conn or DATABASE_URL env
  2. .mcp.json found by walking up from CWD
  3. ~/.claude.json projects[*].mcpServers
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="pg-doc", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    # inventory
    import inventory
    p_inv = sub.add_parser("inventory", help="Table inventory + per-table column docs")
    inventory.add_arguments(p_inv)
    p_inv.set_defaults(func=inventory.run)

    # mv-deps
    import mv_deps
    p_mv = sub.add_parser("mv-deps", help="View/MV base-table dependency analysis")
    mv_deps.add_arguments(p_mv)
    p_mv.set_defaults(func=mv_deps.run)

    # column-search (Phase 2)
    try:
        import column_search
        p_cs = sub.add_parser("column-search", help="Find tables containing a column")
        column_search.add_arguments(p_cs)
        p_cs.set_defaults(func=column_search.run)
    except ImportError:
        pass

    # size (Phase 2)
    try:
        import size as _size
        p_sz = sub.add_parser("size", help="Table size + activity report")
        _size.add_arguments(p_sz)
        p_sz.set_defaults(func=_size.run)
    except ImportError:
        pass

    # lineage (Phase 2)
    try:
        import lineage
        p_ln = sub.add_parser("lineage", help="Recursive view/MV lineage tree")
        lineage.add_arguments(p_ln)
        p_ln.set_defaults(func=lineage.run)
    except ImportError:
        pass

    args = parser.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
