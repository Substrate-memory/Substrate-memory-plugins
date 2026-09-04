#!/bin/sh
# Install the Substrate memory plugin for Grok Bot. POSIX sh, idempotent,
# non-interactive. Safe to re-run.
set -eu
REPO_URL="${SUBSTRATE_REPO_URL:-https://github.com/Substrate-memory/Substrate-memory-plugins.git}"
REF="${SUBSTRATE_REF:-v0.3.0}"
GROK_HOME_DIR="${GROK_HOME:-${SUBSTRATE_HOME:-$HOME/.grok}}"
DEST="$GROK_HOME_DIR/plugins/substrate-memory"
if command -v git >/dev/null 2>&1; then
  if [ -d "$DEST/.git" ]; then
    git -C "$DEST" fetch --quiet origin || true
    git -C "$DEST" checkout --quiet "$REF" || true
  else
    mkdir -p "$GROK_HOME_DIR/plugins"
    if [ -d "$DEST" ]; then
      echo "substrate-memory: $DEST exists and is not a git checkout; leaving it in place" >&2
    else
      if ! git clone --quiet --branch "$REF" --depth 1 "$REPO_URL" "$DEST" 2>/dev/null; then
        git clone --quiet "$REPO_URL" "$DEST"
        git -C "$DEST" checkout --quiet "$REF" || true
      fi
    fi
  fi
else
  mkdir -p "$DEST"
  echo "substrate-memory: git not found; copy plugins/grok-bot here yourself: $DEST" >&2
fi
# Register the MCP stdio server in the Grok MCP client file (JSON), merging
# without clobbering existing servers. Uses python3 when available.
MCP_JSON="$GROK_HOME_DIR/mcp.json"
PLUGIN_DIR="$DEST/plugins/grok-bot"
if [ -d "$PLUGIN_DIR" ] && command -v python3 >/dev/null 2>&1; then
  PLUGIN_DIR="$PLUGIN_DIR" MCP_JSON="$MCP_JSON" python3 -c "
import json, os
mcp = os.environ['MCP_JSON']; plug = os.environ['PLUGIN_DIR']
try:
    with open(mcp, encoding='utf-8') as f: cfg = json.load(f)
    if not isinstance(cfg, dict): cfg = {}
except (OSError, ValueError): cfg = {}
servers = cfg.setdefault('mcpServers', {})
if not isinstance(servers, dict): cfg['mcpServers'] = servers = {}
servers.setdefault('substrate-memory', {'command': 'python3', 'args': [plug + '/server.py']})
os.makedirs(os.path.dirname(mcp) or '.', exist_ok=True)
with open(mcp, 'w', encoding='utf-8') as f: json.dump(cfg, f, indent=2, sort_keys=True); f.write(chr(10))
print('substrate-memory: registered MCP server in ' + mcp)
"
fi
echo "substrate-memory: installed at $DEST"
echo "Next: run setup once (prints a browser approval link on first use):"
echo "  GROK_HOME="$GROK_HOME_DIR" python3 "$DEST/plugins/grok-bot/setup.py""
