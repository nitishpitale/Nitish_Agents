"""
Entry point: python -m mcp_servers.yahoo_finance_news

Modes:
  (default)  stdio transport  — MCP clients launch this as a subprocess.
  --http     streamable-HTTP  — serves at http://127.0.0.1:8001/mcp
             Override host/port with --host and --port flags.

Examples:
  python3 -m mcp_servers.yahoo_finance_news
  python3 -m mcp_servers.yahoo_finance_news --http
  python3 -m mcp_servers.yahoo_finance_news --http --port 9000
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Yahoo Finance News MCP Server",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable-HTTP instead of stdio",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8001, help="HTTP port (default: 8001)")
    args = parser.parse_args()

    # Import the server after arg-parsing so --help works even without yfinance
    from .server import mcp

    if args.http:
        import uvicorn
        app = mcp.streamable_http_app()
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
