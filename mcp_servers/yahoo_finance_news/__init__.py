"""
Yahoo Finance News MCP Server.

Exposes financial news from Yahoo Finance as MCP tools consumable by any
MCP-compatible client (Claude Desktop, Cursor, the volatility-engine pipeline, etc.).

Tools:
  get_ticker_news   — recent news for a specific stock ticker
  get_market_news   — broad market / index news
  search_news       — search Yahoo Finance news by free-text query

Run via:
  python -m mcp_servers.yahoo_finance_news          (stdio, for MCP clients)
  python -m mcp_servers.yahoo_finance_news --http   (streamable-HTTP mode)
"""
