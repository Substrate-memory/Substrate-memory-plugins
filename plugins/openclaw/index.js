/**
 * Substrate memory for OpenClaw.
 *
 * Thin zero-dependency adapter (Node builtins only: node:child_process,
 * node:fs, node:path, node:os). All memory logic lives in the vendored
 * stdlib-only Python core in ./substrate_core/ (byte-identical contract,
 * client, and onboarding plus the host-neutral runtime port). This file only
 * registers the 3 tools and the lifecycle hooks and translates between
 * OpenClaw shapes and the bridge stdio JSON protocol.
 *
 * Tool execute() returns { content: [{ type: "text", text }] } where text is
 * the exact bounded JSON string the Hermes plugin returns, so result shapes
 * match across hosts.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));
const BRIDGE = join(PLUGIN_DIR, "substrate_core", "bridge.py");

// Must stay identical to substrate_core/runtime.py STATIC_MEMORY_PROMPT.
// Checked by tests/test_openclaw_plugin.py.
const STATIC_MEMORY_PROMPT =
  "Substrate memory. Lines in `<memory-context>` are facts from the user's " +
  "knowledge base, selected for this turn. Use them naturally and do not " +
  "announce that you remembered. `[contested]` means sources disagree; call " +
  "`memory_expand` before relying on it. `[as of DATE]` means it may have " +
  "changed. Call `memory_evidence` when the user asks why you believe something. " +
  "Call `memory_search` with your intended action before irreversible operations. " +
  "Pinned pages follow.";

const HANDLE_PATTERN = "^[mp]:[0-9a-f]{8,64}$";

const TOOL_SCHEMAS = {
  memory_search: {
    name: "memory_search",
    label: "Memory Search",
    description:
      "Search Substrate memory. Use the intended action in the query before irreversible operations.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "What to recall or the intended action." },
        kinds: { type: "array", items: { type: "string" }, maxItems: 16 },
        limit: { type: "integer", minimum: 1, maximum: 20, default: 8 },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
  memory_expand: {
    name: "memory_expand",
    label: "Memory Expand",
    description: "Expand a Substrate memory or page handle into its bounded detail.",
    parameters: {
      type: "object",
      properties: { handle: { type: "string", pattern: HANDLE_PATTERN } },
      required: ["handle"],
      additionalProperties: false,
    },
  },
  memory_evidence: {
    name: "memory_evidence",
    label: "Memory Evidence",
    description: "Get evidence excerpts for a Substrate memory handle.",
    parameters: {
      type: "object",
      properties: {
        handle: { type: "string", pattern: HANDLE_PATTERN },
        raw: { type: "boolean", default: false },
        limit: { type: "integer", minimum: 1, maximum: 20, default: 5 },
      },
      required: ["handle"],
      additionalProperties: false,
    },
  },
};

function log(api, level, msg) {
  try {
    if (api && api.logger && typeof api.logger[level] === "function") api.logger[level](msg);
  } catch (_e) {
    /* logging never breaks the turn */
  }
}

function bridgeEnv(api) {
  const env = { ...process.env };
  try {
    const cfg = (api && api.pluginConfig) || {};
    const server = (cfg && cfg.server) || {};
    if (!env.SUBSTRATE_API_URL && typeof server.url === "string" && server.url) {
      env.SUBSTRATE_API_URL = server.url;
    }
    const agentName =
      (typeof cfg.agentName === "string" && cfg.agentName) ||
      (typeof server.agentName === "string" && server.agentName);
    if (!env.SUBSTRATE_AGENT_NAME && agentName) env.SUBSTRATE_AGENT_NAME = agentName;
  } catch (_e) {
    /* config is best effort */
  }
  return env;
}

function pythonBin(api) {
  // Precedence: SUBSTRATE_PYTHON env wins, then the `python` plugin-config
  // key (lowest), then plain `python3`. Never reads secrets.
  try {
    const cfg = (api && api.pluginConfig) || {};
    if (!process.env.SUBSTRATE_PYTHON && typeof cfg.python === "string" && cfg.python) {
      return cfg.python;
    }
  } catch (_e) {
    /* config is best effort */
  }
  return process.env.SUBSTRATE_PYTHON || "python3";
}

function runBridgeChild(api, command, payload, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    let child;
    try {
      if (!existsSync(BRIDGE)) {
        done({ ok: false, error: "invalid_config" });
        return;
      }
      child = spawn(pythonBin(api), [BRIDGE], {
        env: bridgeEnv(api),
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch (_e) {
      done({ ok: false, error: "transport_error" });
      return;
    }
    let out = "";
    const timer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch (_e) {
        /* already exited */
      }
      done({ ok: false, error: "transport_error" });
    }, timeoutMs);
    child.on("error", () => {
      clearTimeout(timer);
      done({ ok: false, error: "transport_error" });
    });
    child.stdout.on("data", (chunk) => {
      out += String(chunk);
      if (out.length > 1048576) {
        try {
          child.kill("SIGKILL");
        } catch (_e) {
          /* already exited */
        }
      }
    });
    child.on("close", () => {
      clearTimeout(timer);
      const text = (out || "").trim();
      if (!text) {
        done({ ok: false, error: "transport_error" });
        return;
      }
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object") done(parsed);
        else done({ ok: false, error: "invalid_response" });
      } catch (_e) {
        done({ ok: false, error: "transport_error" });
      }
    });
    try {
      child.stdin.write(JSON.stringify({ command, payload }));
      child.stdin.end();
    } catch (_e) {
      clearTimeout(timer);
      done({ ok: false, error: "transport_error" });
    }
  });
}

async function callBridge(api, command, payload, timeoutMs) {
  try {
    return await runBridgeChild(api, command, payload, timeoutMs);
  } catch (_e) {
    return { ok: false, error: "transport_error" };
  }
}

function fireBridge(api, command, payload) {
  try {
    if (!existsSync(BRIDGE)) return;
    const child = spawn(pythonBin(api), [BRIDGE], {
      env: bridgeEnv(api),
      stdio: ["pipe", "ignore", "ignore"],
      detached: true,
    });
    child.on("error", () => {});
    try {
      child.stdin.write(JSON.stringify({ command, payload }));
      child.stdin.end();
    } catch (_e) {
      /* best effort */
    }
    child.unref();
  } catch (_e) {
    /* capture failures never block a turn */
  }
}

function textOf(content, maxChars) {
  let text = "";
  if (typeof content === "string") text = content;
  else if (Array.isArray(content)) {
    const parts = [];
    for (const part of content) {
      if (typeof part === "string") parts.push(part);
      else if (part && typeof part === "object") {
        if (typeof part.text === "string") parts.push(part.text);
        else if (typeof part.content === "string") parts.push(part.content);
      }
    }
    text = parts.join("\n");
  } else if (content != null) {
    try {
      text = JSON.stringify(content);
    } catch (_e) {
      text = String(content);
    }
  }
  if (text.length > maxChars) text = text.slice(0, maxChars);
  return text;
}

function toHistoryRows(messages, maxRows, maxChars) {
  if (maxRows === undefined) maxRows = 64;
  if (maxChars === undefined) maxChars = 8000;
  if (!Array.isArray(messages)) return [];
  const rows = [];
  for (const item of messages.slice(-maxRows)) {
    if (!item || typeof item !== "object") continue;
    const role = item.role;
    if (role !== "user" && role !== "assistant" && role !== "tool") continue;
    const row = { role, content: textOf(item.content, maxChars) };
    if (typeof item.timestamp === "string") row.timestamp = item.timestamp;
    if (role === "tool") {
      if (typeof item.tool_call_id === "string") row.tool_call_id = item.tool_call_id.slice(0, 128);
      if (typeof item.id === "string" && !row.tool_call_id) row.tool_call_id = item.id.slice(0, 128);
      if (typeof item.tool_name === "string") row.tool_name = item.tool_name.slice(0, 128);
      else if (typeof item.name === "string") row.tool_name = item.name.slice(0, 128);
    }
    if (role === "assistant" && Array.isArray(item.tool_calls)) row.tool_calls = item.tool_calls;
    rows.push(row);
  }
  return rows;
}

function sessionOf(ctx, event) {
  const sessionId =
    (ctx && (ctx.sessionId || ctx.sessionKey)) || (event && (event.sessionId || event.sessionKey)) || "";
  return typeof sessionId === "string" ? sessionId : "";
}

function errorText(category) {
  const allowed = ["invalid_request", "invalid_response", "invalid_config", "timeout", "transport_error"];
  return JSON.stringify({ error: allowed.indexOf(category) >= 0 ? category : "transport_error" });
}

function toolText(response) {
  if (response && response.ok === true && response.result !== undefined) {
    try {
      return JSON.stringify(response.result);
    } catch (_e) {
      return errorText("invalid_response");
    }
  }
  return errorText(response && response.error);
}

function onHook(api, name, handler) {
  try {
    if (api && typeof api.on === "function") api.on(name, handler);
    else if (api && typeof api.registerHook === "function") api.registerHook(name, handler);
  } catch (_e) {
    log(api, "warn", "[substrate] hook registration failed for " + name);
  }
}

export default function register(api) {
  for (const toolName of ["memory_search", "memory_expand", "memory_evidence"]) {
    const def = TOOL_SCHEMAS[toolName];
    const command = toolName === "memory_search" ? "search" : toolName.slice("memory_".length);
    try {
      api.registerTool(
        {
          name: def.name,
          label: def.label,
          description: def.description,
          parameters: def.parameters,
          execute: async (_toolCallId, params) => {
            const response = await callBridge(api, command, { args: params || {} }, 10000);
            return { content: [{ type: "text", text: toolText(response) }] };
          },
        },
        { name: toolName },
      );
    } catch (_e) {
      log(api, "warn", "[substrate] tool registration failed for " + toolName);
    }
  }

  onHook(api, "before_prompt_build", async (event, ctx) => {
    try {
      const sessionId = sessionOf(ctx, event);
      if (!sessionId) return { appendSystemContext: STATIC_MEMORY_PROMPT };
      const userMessage =
        (event && typeof event.prompt === "string" && event.prompt) ||
        (event && typeof event.cleanedBody === "string" && event.cleanedBody) ||
        "";
      if (!userMessage) return { appendSystemContext: STATIC_MEMORY_PROMPT };
      const out = { appendSystemContext: STATIC_MEMORY_PROMPT };
      const response = await callBridge(
        api,
        "turn_context", 
        {
          session_id: sessionId,
          user_message: userMessage,
          conversation_history: toHistoryRows(event && event.messages),
          platform: "openclaw",
          chat_type: "direct",
          sender_id: (ctx && (ctx.senderId || ctx.chatId)) || "",
          agent_identity: (ctx && ctx.agentId) || "",
        },
        1500,
      );
      if (response && response.ok === true && response.context && response.context.context) {
        out.prependContext = response.context.context;
      }
      return out;
    } catch (_e) {
      return { appendSystemContext: STATIC_MEMORY_PROMPT };
    }
  });

  onHook(api, "agent_end", (event, ctx) => {
    try {
      if (!event || event.success === false) return;
      const sessionId = sessionOf(ctx, event);
      if (!sessionId || !Array.isArray(event.messages) || event.messages.length === 0) return;
      const rows = toHistoryRows(event.messages);
      let lastUser = "";
      let lastAssistant = "";
      for (let i = rows.length - 1; i >= 0; i--) {
        if (!lastUser && rows[i].role === "user") lastUser = rows[i].content;
        if (!lastAssistant && rows[i].role === "assistant") lastAssistant = rows[i].content;
        if (lastUser && lastAssistant) break;
      }
      fireBridge(api, "capture", {
        user_content: lastUser,
        assistant_content: lastAssistant,
        session_id: sessionId,
        messages: rows,
        sender_id: (ctx && (ctx.senderId || ctx.chatId)) || "user",
      });
    } catch (_e) {
      /* capture never blocks */
    }
  });

  const boundaryForReason = (reason) =>
    reason === "new" || reason === "reset" ? "reset" : "end";

  onHook(api, "session_end", (event, ctx) => {
    try {
      const sessionId = sessionOf(ctx, event);
      if (!sessionId) return;
      fireBridge(api, "session", {
        session_id: sessionId,
        boundary: boundaryForReason(event && event.reason),
        platform: "openclaw",
        chat_type: "direct",
        next_session_id: (event && (event.nextSessionId || event.nextSessionKey)) || "",
        high_water: (event && event.messageCount) || 0,
      });
    } catch (_e) {
      /* never blocks */
    }
  });

  onHook(api, "before_reset", (event, ctx) => {
    try {
      const sessionId = sessionOf(ctx, event);
      if (!sessionId) return;
      fireBridge(api, "session", {
        session_id: sessionId,
        boundary: "reset",
        platform: "openclaw",
        chat_type: "direct",
      });
    } catch (_e) {
      /* never blocks */
    }
  });

  onHook(api, "session_start", (event, ctx) => {
    try {
      const resumedFrom = event && (event.resumedFrom || event.previousSessionId);
      const sessionId = sessionOf(ctx, event);
      if (!resumedFrom || typeof resumedFrom !== "string") return;
      fireBridge(api, "session", {
        session_id: resumedFrom,
        boundary: "switch",
        platform: "openclaw",
        chat_type: "direct",
        next_session_id: sessionId,
      });
    } catch (_e) {
      /* never blocks */
    }
  });

  log(api, "info", "[substrate] memory plugin registered: 3 tools + turn-context/capture/session hooks");
}

// Named export for offline parity tests: lets the Python suite import the
// exact TOOL_SCHEMAS objects the adapter registers and deep-compare them
// against the Hermes reference schemas. Harmless to the host loader, which
// uses the default `register` export.
export { TOOL_SCHEMAS, STATIC_MEMORY_PROMPT };
