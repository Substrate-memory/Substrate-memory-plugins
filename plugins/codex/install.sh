#!/bin/sh
# Substrate memory installer for OpenAI Codex CLI (POSIX sh).
# Idempotent and non-interactive. Safe to re-run.
#
# Usage:
#   sh plugins/codex/install.sh [--checkout DIR] [--mcp-name NAME]
#
# What it does:
#   1. Checks python3 (>= 3.11) and `codex` are available.
#   2. Registers the Substrate MCP stdio server with Codex
#      (`codex mcp add substrate -- python3 <checkout>/plugins/codex/server.py`).
#   3. Prints verification and rollback steps. It never asks for keys and
#      never disables TLS verification.
set -eu

CHECKOUT=""
MCP_NAME="substrate"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --checkout) CHECKOUT="$2"; shift 2 ;;
    --mcp-name) MCP_NAME="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: install.sh [--checkout DIR] [--mcp-name NAME]"
      exit 0
      ;;
    *)
      echo "install.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$CHECKOUT" ]; then
  # Directory holding this script is <checkout>/plugins/codex.
  SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  CHECKOUT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
fi

SERVER="$CHECKOUT/plugins/codex/server.py"
if [ ! -f "$SERVER" ]; then
  echo "install.sh: MCP server not found: $SERVER" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "install.sh: python3 is required" >&2
  exit 1
fi
PY_OK="$(python3 -c 'import sys; print("yes" if sys.version_info >= (3, 11) else "no")')"
if [ "$PY_OK" != "yes" ]; then
  echo "install.sh: python3 >= 3.11 is required" >&2
  exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "install.sh: the Codex CLI is required on PATH" >&2
  exit 1
fi

# The CLI refuses a CODEX_HOME that does not exist yet; create it privately.
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
if [ ! -d "$CODEX_HOME_DIR" ]; then
  mkdir -p "$CODEX_HOME_DIR"
  chmod 0700 "$CODEX_HOME_DIR"
fi

# Idempotent: drop a previous registration of the same name, if any.
codex mcp remove "$MCP_NAME" >/dev/null 2>&1 || true
codex mcp add "$MCP_NAME" -- python3 "$SERVER"

echo "Substrate MCP server registered as '$MCP_NAME'."
echo "Verify with: codex mcp get $MCP_NAME"
echo "Rollback with: codex mcp remove $MCP_NAME"
echo "First memory use triggers one-time browser approval; show the user the exact link."
