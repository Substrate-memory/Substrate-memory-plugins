"""Cross-repo integration: plugin tools against a real isolated server.

Boots the actual Substrate Node server from an explicit repo path
(``SUBSTRATE_SERVER_REPO``, default ``/tmp/wt-hermes-durability``) as a
subprocess with temp homes and an ephemeral loopback port, then drives
the full lifecycle through the real plugin tools over HTTP with a
freshly minted test bearer: remember -> search -> evidence -> forget ->
search exclusion + evidence retained -> remember revives the same
handle. No mocks, no live endpoints, no production secrets. Skipped when
the server checkout or node is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SERVER_REPO = Path(
    os.environ.get("SUBSTRATE_SERVER_REPO", "/tmp/wt-hermes-durability")
)
BOOT_JS = r"""
// Isolated Substrate server for plugin integration tests (test-only).
// Mirrors the boot() in memory-ledger.test.mjs: temp homes, stub billing,
// minted test bearer, ephemeral loopback port. No production secrets,
// no external calls. Prints one JSON line {base, token} when listening.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import http from "node:http";
import { openControlStore } from "__REPO__/control-store.mjs";
import { createApplication } from "__REPO__/server.mjs";
import { createTenantManager } from "__REPO__/tenant-manager.mjs";
import { createAgentOnboarding } from "__REPO__/agent-auth.mjs";

const TOKEN = "sk_sub_plugin_integration_test_token_xyz789";

async function boot() {
  const dir = mkdtempSync(join(tmpdir(), "substrate-plugin-int-"));
  const control = openControlStore({ dataDirectory: dir });
  const billing = {
    async getStatus() { return { required: true, active: true, status: "active", priceId: "price_test" }; },
    async createCheckoutSession() { return "https://checkout.test"; },
    async handleWebhook() { return { processed: true }; },
  };
  const tenant = control.provisionTenant("user_plugin_int");
  control.setBilling({ tenantId: tenant.tenantId, provider: "stripe", entitlement: "active", status: "active", priceId: "price_test" });
  control.activateTenantForAccount("user_plugin_int");
  const tenants = createTenantManager({ rootDir: control.tenantsDirectory, publicOrigin: "http://127.0.0.1", backgroundJobs: false });
  const { createHash } = await import("node:crypto");
  const tokenHash = createHash("sha256").update(TOKEN).digest("hex");
  await tenants.withRuntime(control.getTenant(tenant.tenantId), runtime => runtime.registerAgentCredential({
    tokenHash, tokenPrefix: TOKEN.slice(0, 15), scopes: "capture retrieve", agentId: "agent_plugin_int",
  }));
  control.registerAgentCredential({ tenantId: tenant.tenantId, bearerToken: TOKEN, agentId: "agent_plugin_int", scopes: ["capture", "retrieve"] });
  const registerAgent = async input => {
    const resolved = control.getTenant(input.tenantId);
    const tokenHashInner = createHash("sha256").update(input.bearerToken).digest("hex");
    await tenants.withRuntime(resolved, runtime => runtime.registerAgentCredential({
      tokenHash: tokenHashInner, tokenPrefix: input.bearerToken.slice(0, 15), scopes: input.scopes, agentId: input.agentId,
    }));
    control.registerAgentCredential({ ...input, scopes: input.scopes.split(" ") });
  };
  const agentOnboarding = createAgentOnboarding({ db: control.db, publicOrigin: "http://127.0.0.1", registerAgent });
  const humanAuth = { async authenticateRequest() { throw new Error("not used"); } };
  const app = createApplication({ control, tenants, humanAuth, billing, agentOnboarding, publicOrigin: "http://127.0.0.1" });
  // Test-only probe (harness, not server code): reports whether a session
  // has materialized into a source and how often expected text occurs.
  const probe = async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (url.pathname === "/__test/materialized") {
      const sid = url.searchParams.get("session_id") || "";
      const needle = url.searchParams.get("needle") || "";
      const out = { source_kind: null, occurrences: 0 };
      try {
        await tenants.withRuntime(control.getTenant(tenant.tenantId), async runtime => {
          const row = runtime.db.prepare(
            "SELECT source_kind, current_version_id FROM sources WHERE ingest_session_id=?"
          ).get(sid);
          if (!row) return;
          out.source_kind = row.source_kind;
          if (needle && row.current_version_id) {
            const ver = runtime.db.prepare(
              "SELECT raw_path FROM source_versions WHERE version_id=?"
            ).get(row.current_version_id);
            if (ver && ver.raw_path) {
              const { readFileSync } = await import("node:fs");
              const content = readFileSync(ver.raw_path, "utf8");
              out.occurrences = content.split(needle).length - 1;
            }
          }
        });
      } catch (error) {
        res.writeHead(500, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: String(error && error.message ? error.message : error) }));
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(out));
      return;
    }
    return app.handle(req, res);
  };
  const server = http.createServer(probe);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const ready = { base: `http://127.0.0.1:${server.address().port}`, token: TOKEN };
  console.log(JSON.stringify(ready));
  if (process.env.SUBSTRATE_READY_FILE) {
    const { writeFileSync } = await import("node:fs");
    writeFileSync(process.env.SUBSTRATE_READY_FILE, JSON.stringify(ready));
  }
  const shutdown = () => {
    server.close(() => {
      try { tenants.closeAll(); } catch {}
      try { control.close(); } catch {}
      process.exit(0);
    });
    setTimeout(() => process.exit(0), 5000).unref();
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

boot().catch(error => {
  console.error("boot failed:", error && error.message ? error.message : error);
  process.exit(1);
});
"""

NODE = shutil.which("node")
CLAIM = "The harbor ferry departs at half past six."
REASON = "schedule changed to seven"


def _server_available() -> bool:
    return (
        NODE is not None
        and SERVER_REPO.is_dir()
        and (SERVER_REPO / "server.mjs").is_file()
    )


def _minimal_env(home: Path, ready_file: Path) -> dict:
    """No inherited secrets: the server cannot reach anything external.

    Only loopback + temp homes exist for the child. LLM/provider keys are
    absent by construction (not merely unset once), and billing/human-auth
    are in-boot stubs, so no external call is possible.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home),
        "NODE_ENV": "test",
        "NO_PROXY": "*",
        "SUBSTRATE_READY_FILE": str(ready_file),
    }


def _read_ready(ready_file: Path, proc, timeout=60.0):
    """Poll the readiness file; never block on a pipe. Terminates first."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        try:
            raw = ready_file.read_text()
            if raw.strip():
                return json.loads(raw)
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    return None


def _stop(proc, stdout_path: Path, stderr_path: Path) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10.0)
    try:
        err = stderr_path.read_bytes()[-2000:].decode("utf-8", "replace")
        if proc.returncode not in (0, None, -15) and err.strip():
            print(f"\nserver stderr tail:\n{err}")
    except OSError:
        pass


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Isolated server subprocess. Only this fixture's process is managed."""
    if not _server_available():
        pytest.skip("server checkout or node unavailable")
    import shutil as _shutil

    work = tmp_path_factory.mktemp("srv")
    (work / "boot.mjs").write_text(BOOT_JS.replace("__REPO__", str(SERVER_REPO)))
    tmp_base = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
    home = Path(tempfile.mkdtemp(prefix="substrate-srv-", dir=str(tmp_base)))
    ready_file = work / "ready.json"
    stdout_path = work / "server.out.log"
    stderr_path = work / "server.err.log"
    out_handle = stdout_path.open("wb")
    err_handle = stderr_path.open("wb")
    proc = subprocess.Popen(
        [NODE, str(work / "boot.mjs")],
        stdout=out_handle,
        stderr=err_handle,
        cwd=str(work),
        env=_minimal_env(home, ready_file),
    )
    try:
        info = _read_ready(ready_file, proc, timeout=60.0)
        assert info and info.get("base"), "server never became ready"
        yield info["base"], info["token"]
    finally:
        _stop(proc, stdout_path, stderr_path)
        out_handle.close()
        err_handle.close()
        _shutil.rmtree(home, ignore_errors=True)


def _wait_until(predicate, timeout=25.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def _handles(search_result):
    return [item["handle"] for item in search_result.get("results", [])]


def test_remember_search_forget_revive_against_live_server(
    live_server, real_spool, monkeypatch
):
    from substrate import plugin

    base, token = live_server
    monkeypatch.setenv("SUBSTRATE_API_URL", base)
    monkeypatch.setenv("SUBSTRATE_API_KEY", token)
    monkeypatch.delenv("SUBSTRATE_SPOOL_DIR", raising=False)

    remembered = json.loads(
        plugin.memory_remember({"text": CLAIM, "durability": "durable"})
    )
    handle = remembered["handle"]
    assert handle.startswith("m:")

    found = json.loads(plugin.memory_search({"query": "harbor ferry half past six"}))
    assert handle in _handles(found)

    evidence = json.loads(plugin.memory_evidence({"handle": handle}))
    assert any("half past six" in json.dumps(item) for item in evidence["excerpts"])

    forgotten = json.loads(
        plugin.memory_forget({"handle": handle, "reason": REASON})
    )
    assert forgotten == {"handle": handle}

    excluded = json.loads(plugin.memory_search({"query": "harbor ferry half past six"}))
    assert handle not in _handles(excluded)

    retained = json.loads(plugin.memory_evidence({"handle": handle}))
    assert retained["excerpts"], "evidence must survive invalidation"
    assert any(REASON in json.dumps(item) for item in retained["excerpts"])

    revived = json.loads(
        plugin.memory_remember({"text": CLAIM, "durability": "durable"})
    )
    assert revived == {"handle": handle}, "revival must reuse the same handle"

    found_again = json.loads(
        plugin.memory_search({"query": "harbor ferry half past six"})
    )
    assert handle in _handles(found_again)


TURN_TEXT = "The turntable spins at midnight"


def test_completed_turn_plus_finalize_materializes_short_session(
    live_server, real_spool, monkeypatch, clean_session_state
):
    """Capture path acceptance: one plugin turn + true finalize.

    Drives the real capture path (post_llm_call -> sync_turn -> spool ->
    sender -> server), then the true session boundary. Proves queueing
    (turn ACK), the sealed boundary, spool ACK retirement, and server-side
    source materialization with deduped text. Extraction/search recall is
    NOT claimed here (no LLM in the loop); the explicit-memory roundtrip
    above remains the recall gate.
    """
    import urllib.request

    from substrate import plugin
    import substrate.spool as spool_module

    base, token = live_server
    monkeypatch.setenv("SUBSTRATE_API_URL", base)
    monkeypatch.setenv("SUBSTRATE_API_KEY", token)
    monkeypatch.delenv("SUBSTRATE_SPOOL_DIR", raising=False)
    session = "live-plugin-session"

    plugin.post_llm_call(
        session_id=session,
        task_id="task-1",
        turn_id="turn-1",
        user_message=f"Remember this: {TURN_TEXT}.",
        assistant_response=f"Saved: {TURN_TEXT}.",
        conversation_history=[
            {"role": "user", "content": f"Remember this: {TURN_TEXT}."},
            {"role": "assistant", "content": f"Saved: {TURN_TEXT}."},
        ],
        model="m",
        platform="cli",
    )
    plugin.on_session_finalize(session, platform="cli", reason="shutdown")

    spool = spool_module.get_spool()
    assert _wait_until(lambda: spool.pending() == 0, timeout=25.0), (
        "sender never retired the turn + boundary (ACK retirement failed)"
    )

    def probe():
        url = (
            f"{base}/__test/materialized?session_id={session}"
            f"&needle={TURN_TEXT.replace(' ', '%20')}"
        )
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    assert _wait_until(
        lambda: probe()["source_kind"] == "hermes_history", timeout=25.0
    ), "short session never materialized a source"
    assert probe()["occurrences"] == 2, (
        "materialized text must carry both turn rows exactly once"
    )
