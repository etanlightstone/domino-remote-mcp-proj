#!/bin/bash
# Domino App entry point for the Remote MCP Server
#
# This script is called by Domino when starting the app.
# The server MUST bind to 0.0.0.0:8888 (Domino's required port for apps).
#
# Environment variables (set in Domino project settings or app config):
#   MCP_AUTH_MODE   — "app_owner" (default) or "user_token"
#   MCP_PORT        — defaults to 8888 (don't change for Domino apps)

set -e

echo "=== Domino Remote MCP Server ==="
echo "Installing dependencies..."
pip install -q -r requirements.txt

echo "Starting MCP server on port ${MCP_PORT:-8888}..."
exec python domino_mcp_server.py
