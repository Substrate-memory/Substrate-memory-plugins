"""Durability tests for the write-ahead spool (spec section 4).

Every test uses an isolated temp directory; the live DB is never touched.
"""

from __future__ import annotations

import json
import os
import pathlib as _pl
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import uuid

import pytest

from substrate import spool as spool_mod
from substrate.client import ClientError, SubstrateClient
from substrate.spool import (
    PRIORITY_CATCHUP,
    PRIORITY_EXPLICIT,
    PRIORITY_LIVE,
    PRIORITY_REPLAY,
    Spool,
    SpoolFull,
    get_spool,
    reset_spool,
)


def make_envelope(event_id=None, *, kind="capture_turn", session_id="session-1"):
    return {
        "schema_version": 3,
        "contract_version": 1,
        "event_id": event_id or str(uuid.uuid4()),
        "kind": kind,
        "session_id": session_id,
        "offset": {"start": 0, "end": 1},
        "capture_origin": "live",
        "batch_id": "",
        "speaker": {"id": "user", "role": "owner", "display": ""},
        "created_at": "2026-09-04T00:00:00Z",
        "payload": {"turn_id": "turn-1", "messages": [
            {"index": 0, "role": "user", "content": "hello"},
        ]},
    }


def ack_for(event_id, action="stored"):
    return {
        "event_id": event_id,
        "accepted": True,
        "stored": True,
        "status": "accepted",
        "action": action,
        "handle": "m:12345678",
    }


class FakeClient:
    """Scripted stand-in: a list of dicts (ACKs) / ClientErrors / callables,
    a single catch-all callable, or None (always ACK)."""

    def __init__(self, script=None):
        if callable(script):
            self.func = script
            self.script = []
        else:
            self.func = None
            self.script = list(script) if script is not None else []
        self.calls = []
        self.lock = threading.Lock()

    def post_json(self, path, body, **kwargs):
        with self.lock:
            self.calls.append({
                "path": path,
                "event_id": body.get("event_id"),
                "idempotency_key": kwargs.get("idempotency_key"),
                "timeout": kwargs.get("timeout"),
            })
        if self.func is not None:
            return self.func(path, body, kwargs)
        if not self.script:
            return ack_for(body["event_id"])
        next_item = self.script.pop(0)
        if isinstance(next_item, BaseException):
            raise next_item
        if callable(next_item):
            return next_item(path, body, kwargs)
        return next_item


def new_spool(tmp_path, **kwargs):
    root = tmp_path / f"spool-{uuid.uuid4().hex[:8]}"
    params = {"max_items": 1000, "max_bytes": 10 * 1024 * 1024}
    params.update(kwargs)
    return Spool(root, **params)


def drain(spool, timeout=10.0):
    deadline = time.monotonic() + timeout
    while spool.pending() and time.monotonic() < deadline:
        time.sleep(0.05)
    return spool.pending()


def test_priority_constants_match_contract():
    assert (PRIORITY_EXPLICIT, PRIORITY_LIVE, PRIORITY_CATCHUP, PRIORITY_REPLAY) == (0, 1, 2, 3)


def test_enqueue_returns_id_and_sender_delivers_priority_order(tmp_path):
    sp = new_spool(tmp_path)
    try:
        ids = {}
        for prio, name in [(PRIORITY_REPLAY, "r"), (PRIORITY_LIVE, "l"),
                           (PRIORITY_CATCHUP, "c"), (PRIORITY_EXPLICIT, "e")]:
            env = make_envelope()
            ids[name] = env["event_id"]
            sp.enqueue(env, priority=prio, kind="capture_turn", capture_origin="live")
            time.sleep(0.01)
        # FIFO within a priority: two more live items keep insertion order.
        extra = [make_envelope() for _ in range(2)]
        for env in extra:
            sp.enqueue(env, priority=PRIORITY_LIVE, kind="capture_turn", capture_origin="live")
            time.sleep(0.01)
        order = [ids["e"], ids["l"]] + [e["event_id"] for e in extra] + [ids["c"], ids["r"]]
        seen = []

        def record(path, body, kwargs):
            seen.append(body["event_id"])
            assert kwargs["idempotency_key"] == body["event_id"]
            assert kwargs["timeout"] == 5.0
            return ack_for(body["event_id"])

        sp.start(FakeClient([record] * 6))
        assert drain(sp) == 0
        assert seen == order
        counters = sp.counters()
        delivered = sum(v["item_count"] for k, v in counters.items() if k.endswith("|delivered"))
        assert delivered == 6
    finally:
        sp.close()


def test_history_replay_evicted_first_under_pressure(tmp_path):
    sp = new_spool(tmp_path, max_items=4)
    try:
        sp.enqueue(make_envelope(), priority=PRIORITY_REPLAY,
                   kind="capture_turn", capture_origin="history_replay")
        sp.enqueue(make_envelope(), priority=PRIORITY_REPLAY,
                   kind="capture_turn", capture_origin="history_replay")
        live = make_envelope()
        sp.enqueue(live, priority=PRIORITY_LIVE, kind="capture_turn", capture_origin="live")
        catchup = make_envelope()
        sp.enqueue(catchup, priority=PRIORITY_CATCHUP,
                   kind="capture_turn", capture_origin="catchup")
        # Full: one more live arrival must evict the oldest replay item.
        sp.enqueue(make_envelope(), priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        assert sp.pending() == 4
        counters = sp.counters()
        evicted = {k: v for k, v in counters.items() if k.endswith("|evicted")}
        assert len(evicted) == 1
        key = next(iter(evicted))
        assert key.startswith("capture_turn|history_replay|3|")
        assert evicted[key]["item_count"] == 1
    finally:
        sp.close()


def test_explicit_never_silently_dropped(tmp_path):
    sp = new_spool(tmp_path, max_items=2)
    try:
        # Fill with replay, then an explicit op evicts replay and returns normally.
        sp.enqueue(make_envelope(), priority=PRIORITY_REPLAY,
                   kind="capture_turn", capture_origin="history_replay")
        sp.enqueue(make_envelope(), priority=PRIORITY_REPLAY,
                   kind="capture_turn", capture_origin="history_replay")
        explicit = make_envelope(kind="memory_write")
        item_id = sp.enqueue(explicit, priority=PRIORITY_EXPLICIT,
                             kind="memory_write", capture_origin="live")
        assert isinstance(item_id, str) and item_id
        assert sp.pending() == 2
        # Fill with explicit only: the next explicit op raises SpoolFull and
        # everything stays spooled.
        sp2_root = tmp_path / "explicit-only"
        sp2 = Spool(sp2_root, max_items=1)
        try:
            sp2.enqueue(make_envelope(kind="memory_write"), priority=PRIORITY_EXPLICIT,
                        kind="memory_write", capture_origin="live")
            with pytest.raises(SpoolFull):
                sp2.enqueue(make_envelope(kind="memory_forget"), priority=PRIORITY_EXPLICIT,
                            kind="memory_forget", capture_origin="live")
            assert sp2.pending() == 1
            counters = sp2.counters()
            full = [v for k, v in counters.items() if k.endswith("|spool_full")]
            assert full and full[0]["item_count"] == 1
        finally:
            sp2.close()
    finally:
        sp.close()


def test_never_evict_in_flight(tmp_path):
    sp = new_spool(tmp_path, max_items=2)
    try:
        first = make_envelope()
        sp.enqueue(first, priority=PRIORITY_REPLAY,
                   kind="capture_turn", capture_origin="history_replay")
        second = make_envelope()
        sp.enqueue(second, priority=PRIORITY_REPLAY,
                   kind="capture_turn", capture_origin="history_replay")
        claimed = sp.claim()
        assert claimed["event_id"] == first["event_id"]
        # The first item is in-flight while the arrival lands, so only the
        # unclaimed second item may be evicted.
        third = make_envelope()
        sp.enqueue(third, priority=PRIORITY_REPLAY,
                   kind="capture_turn", capture_origin="history_replay")
        assert sp.pending() == 2
        remaining = {claimed["event_id"]}
        item = sp.claim()
        assert item is not None
        remaining.add(item["event_id"])
        assert first["event_id"] in remaining
        assert third["event_id"] in remaining
        assert second["event_id"] not in remaining
    finally:
        sp.close()


def test_crash_mid_flight_loses_nothing(tmp_path):
    root = tmp_path / "crash"
    sp = Spool(root, max_items=100)
    try:
        events = [make_envelope() for _ in range(3)]
        for env in events:
            sp.enqueue(env, priority=PRIORITY_LIVE,
                       kind="capture_turn", capture_origin="live")
        claimed = sp.claim()
        assert claimed is not None
        # Simulated crash: never release/stop; a fresh handle reopens the root.
        crashed = Spool(root)
        try:
            assert crashed.pending() == 3
            crashed.start(FakeClient([]))
            assert drain(crashed) == 0
            counters = crashed.counters()
            delivered = sum(v["item_count"] for k, v in counters.items()
                            if k.endswith("|delivered"))
            assert delivered == 3
        finally:
            crashed.close()
    finally:
        sp.close()


def test_every_retired_item_has_ack_or_durable_counter(tmp_path):
    sp = new_spool(tmp_path, max_items=3)
    try:
        good = [make_envelope() for _ in range(2)]
        for env in good:
            sp.enqueue(env, priority=PRIORITY_LIVE,
                       kind="capture_turn", capture_origin="live")
        doomed = make_envelope()
        sp.enqueue(doomed, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")

        def script(path, body, kwargs):
            if body["event_id"] == doomed["event_id"]:
                raise ClientError("invalid_request", transient=False)
            return ack_for(body["event_id"])

        sp.start(FakeClient([script] * 3))
        assert drain(sp) == 0
        counters = sp.counters()
        delivered = sum(v["item_count"] for k, v in counters.items() if k.endswith("|delivered"))
        quarantined = sum(v["item_count"] for k, v in counters.items()
                          if k.endswith("|quarantined"))
        assert delivered == 2
        assert quarantined == 1
    finally:
        sp.close()


@pytest.mark.parametrize("bad_ack", [
    {},
    {"stored": False, "event_id": "x", "action": "stored"},
    {"stored": "true", "event_id": "x", "action": "stored"},
    {"stored": True, "event_id": "other-id", "action": "stored"},
    {"stored": True, "event_id": "x", "action": "mystery"},
    {"stored": True, "event_id": "x"},
    {"ok": True},
])
def test_ack_semantics_never_retire_on_bare_200(tmp_path, bad_ack):
    sp = new_spool(tmp_path)
    try:
        env = make_envelope()
        bad = dict(bad_ack)
        if bad.get("event_id") == "x":
            bad["event_id"] = env["event_id"]
        sp.enqueue(env, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        sp.start(FakeClient(lambda path, body, kwargs: dict(bad)))
        time.sleep(1.5)
        # Still spooled: a bad ACK is a transient failure, never a retire.
        assert sp.pending() == 1
        counters = sp.counters()
        assert not [k for k in counters if k.endswith("|delivered")]
    finally:
        sp.close()


@pytest.mark.parametrize("action", ["stored", "duplicate", "sealed", "queued"])
def test_ack_semantics_valid_actions_retire(tmp_path, action):
    sp = new_spool(tmp_path)
    try:
        env = make_envelope()
        response = ack_for(env["event_id"], action=action)
        response["extra_future_field"] = "ignored"
        sp.enqueue(env, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        sp.start(FakeClient([response]))
        assert drain(sp) == 0
    finally:
        sp.close()


def test_counters_survive_reopen(tmp_path):
    root = tmp_path / "reopen"
    sp = Spool(root, max_items=2)
    env = make_envelope()
    sp.enqueue(env, priority=PRIORITY_LIVE, kind="capture_turn", capture_origin="live")
    sp.enqueue(make_envelope(), priority=PRIORITY_REPLAY,
               kind="capture_turn", capture_origin="history_replay")
    sp.enqueue(make_envelope(), priority=PRIORITY_LIVE,
               kind="capture_turn", capture_origin="live")
    before = sp.counters()
    sp.close()
    again = Spool(root, max_items=2)
    try:
        assert again.counters() == before
        assert again.pending() == 2
    finally:
        again.close()


def test_bytes_bound_evicts_oldest_lowest(tmp_path):
    big = "x" * 4000
    sp = new_spool(tmp_path, max_items=1000, max_bytes=9000)
    try:
        first = make_envelope()
        first["payload"]["messages"][0]["content"] = big
        sp.enqueue(first, priority=PRIORITY_REPLAY,
                   kind="capture_turn", capture_origin="history_replay")
        live = make_envelope()
        live["payload"]["messages"][0]["content"] = big
        sp.enqueue(live, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        arrival = make_envelope()
        arrival["payload"]["messages"][0]["content"] = big
        sp.enqueue(arrival, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        survivors = set()
        for _ in range(10):
            item = sp.claim()
            if item is None:
                break
            survivors.add(item["event_id"])
        assert first["event_id"] not in survivors
        assert live["event_id"] in survivors and arrival["event_id"] in survivors
    finally:
        sp.close()


def test_retry_backoff_bases_cap_and_retry_after():
    rng = __import__("random").Random(7)
    first = spool_mod.retry_delay("transport_error", 1, rng=rng)
    assert 0.8 <= first <= 1.2
    auth = spool_mod.retry_delay("unauthorized", 1, rng=rng)
    assert 24.0 <= auth <= 36.0
    capped = spool_mod.retry_delay("transport_error", 100, rng=rng)
    assert capped <= 300.0
    floored = spool_mod.retry_delay("transport_error", 1, retry_after=120.0, rng=rng)
    assert floored >= 120.0
    # Server floor is honored even above the 300 s backoff cap.
    assert spool_mod.retry_delay("rate_limited", 1, retry_after=600.0, rng=rng) >= 600.0
    assert spool_mod.retry_delay("transport_error", 100, rng=rng) <= 300.0
    assert spool_mod.is_transient_category("timeout")
    assert spool_mod.is_transient_category("http_503")
    assert spool_mod.is_transient_category("unauthorized")
    assert not spool_mod.is_transient_category("invalid_request")
    assert not spool_mod.is_transient_category("payload_too_large")


def test_transient_failures_retry_then_deliver(tmp_path):
    sp = new_spool(tmp_path)
    try:
        env = make_envelope()
        sp.enqueue(env, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        flaky = FakeClient([
            ClientError("transport_error"),
            ClientError("rate_limited", retry_after=0.01),
            ack_for(env["event_id"]),
        ])
        sp.start(flaky)
        assert drain(sp) == 0
        assert len(flaky.calls) == 3
    finally:
        sp.close()


def test_file_permissions_and_path_safety(tmp_path):
    sp = new_spool(tmp_path)
    try:
        env = make_envelope()
        sp.enqueue(env, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        assert stat.S_IMODE(os.stat(sp.root).st_mode) == 0o700
        files = list(sp.root.glob("*.json"))
        assert files
        for path in files:
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(sp.db_path).st_mode) == 0o600
        with pytest.raises(ValueError):
            spool_mod._safe_child(sp.root, sp.root.parent / "escape.json")
        link = sp.root / "evil.json"
        try:
            os.symlink(str(files[0]), link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
        with pytest.raises(ValueError):
            spool_mod._safe_child(sp.root, link)
        link.unlink()
    finally:
        sp.close()


def test_corrupt_file_is_quarantined_with_counter(tmp_path):
    # A crash may leave an orphan write-ahead file with no DB row (written
    # before COMMIT). Reopening adopts valid orphans and quarantines garbage.
    root = tmp_path / "orphan"
    sp = Spool(root)
    try:
        env = make_envelope()
        sp.enqueue(env, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        sp.close()
        (root / "999-orphan.json").write_bytes(b"{not valid json")
        mode = os.stat(root / "999-orphan.json").st_mode
        os.chmod(root / "999-orphan.json", mode)
        sp2 = Spool(root)
        try:
            assert sp2.pending() == 1  # only the valid row; garbage quarantined
            counters = sp2.counters()
            quarantined = sum(v["item_count"] for k, v in counters.items()
                              if k.endswith("|quarantined"))
            assert quarantined == 1
            assert list((root / "corrupt").glob("*.bad"))
            # The surviving valid item still delivers with an ACK.
            sp2.start(FakeClient([]))
            assert drain(sp2) == 0
        finally:
            sp2.close()
    finally:
        try:
            sp.close()
        except Exception:  # noqa: BLE001 - already closed above
            pass


def test_permanent_404_quarantines_instead_of_retrying(tmp_path):
    sp = new_spool(tmp_path)
    try:
        env = make_envelope(kind="memory_forget")
        sp.enqueue(env, priority=PRIORITY_EXPLICIT,
                   kind="memory_forget", capture_origin="live")
        # Real signal: ClientError("transport_error", status=404, transient=False).
        client = FakeClient([ClientError("transport_error", status=404, transient=False)] * 5)
        sp.start(client)
        deadline = time.monotonic() + 10.0
        while sp.pending() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sp.pending() == 0
        # Quarantined on the first permanent failure: exactly one POST.
        assert len(client.calls) == 1
        counters = sp.counters()
        quarantined = sum(v["item_count"] for k, v in counters.items()
                          if k.endswith("|quarantined"))
        assert quarantined == 1
    finally:
        sp.close()


def test_malformed_200_stays_spooled_and_retries(tmp_path):
    """Actual malformed HTTP 200 bodies via the real client: the item stays
    spooled (transient per the ACK rule) and delivers once valid."""
    import urllib.request as urlrequest

    env = make_envelope()

    class GarbageThenAck:
        status = 200
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return "https://memory.example/api/v1/ledger/events"

        def read(self, size=-1):
            type(self).calls += 1
            if type(self).calls < 3:
                return b"{not json"
            return json.dumps(ack_for(env["event_id"])).encode()

    real = urlrequest.urlopen
    urlrequest.urlopen = lambda request, timeout: GarbageThenAck()
    try:
        # The real client maps the garbage body to permanent invalid_response.
        client = SubstrateClient("https://memory.example", "k")
        with pytest.raises(ClientError) as garbage_info:
            client.post_json("/api/v1/ledger/events", {"a": 1}, timeout=1.0)
        assert garbage_info.value.category == "invalid_response"
        assert garbage_info.value.transient is False
    finally:
        urlrequest.urlopen = real

    GarbageThenAck.calls = 0
    sp = new_spool(tmp_path)
    try:
        sp.enqueue(env, priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        urlrequest.urlopen = lambda request, timeout: GarbageThenAck()
        try:
            client = SubstrateClient("https://memory.example", "k")
            sp.start(client)
            assert drain(sp) == 0
        finally:
            urlrequest.urlopen = real
        # Two transient garbage attempts, then the ACK: never quarantined.
        assert GarbageThenAck.calls == 3
        counters = sp.counters()
        assert sum(v["item_count"] for k, v in counters.items()
                   if k.endswith("|delivered")) == 1
        assert not [k for k in counters if k.endswith("|quarantined")]
    finally:
        sp.close()




REPO_ROOT = _pl.Path(__file__).resolve().parents[1]
SRC_DIR = str(REPO_ROOT / "plugins" / "substrate" / "src")


def _run_native(script, *args, timeout=30):
    """Run a fresh interpreter (only processes this test creates)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# Phase A: enqueue three items, claim one (in-flight), then die abruptly
# with os._exit -- no close, no stop, WAL uncheckpointed.
_KILL_MID_FLIGHT_A = """
import json, os, sys, uuid
sys.path.insert(0, sys.argv[1])
from substrate.spool import Spool
root, manifest = sys.argv[2], sys.argv[3]
sp = Spool(root, max_items=100)
envelopes = []
for i in range(3):
    env = {
        "schema_version": 3, "contract_version": 1,
        "event_id": str(uuid.uuid4()), "kind": "capture_turn",
        "session_id": "session-1", "offset": {"start": i, "end": i + 1},
        "capture_origin": "live", "batch_id": "",
        "speaker": {"id": "user", "role": "owner", "display": ""},
        "created_at": "2026-09-04T00:00:00Z",
        "payload": {"turn_id": f"turn-{i}", "messages": [
            {"index": i, "role": "user", "content": f"hello-{i}"}]},
    }
    sp.enqueue(env, priority=1, kind="capture_turn", capture_origin="live")
    envelopes.append(env)
claimed = sp.claim()
data = {"envelopes": envelopes, "claimed_id": claimed["event_id"]}
with open(manifest, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(data, sort_keys=True))
    fh.flush()
    os.fsync(fh.fileno())
os._exit(17)
"""

# Phase B (fresh process): verify ids/payload/counters, then deliver all.
_RECOVER_AND_DRAIN_B = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from substrate.spool import Spool
root, manifest, out = sys.argv[2], sys.argv[3], sys.argv[4]
expected = json.load(open(manifest, encoding="utf-8"))
sp = Spool(root, max_items=100)
if "envelopes" in expected:
    _envelopes = expected["envelopes"]
else:
    _envelopes = [expected["control"], expected["orphan"]]
wanted = {e["event_id"]: e for e in _envelopes}

class AckClient:
    def __init__(self):
        self.seen = {}
    def post_json(self, path, body, **kw):
        assert kw.get("idempotency_key") == body["event_id"]
        canon = json.dumps(body, ensure_ascii=False, separators=(",", ":"),
                           sort_keys=True)
        self.seen[body["event_id"]] = canon
        return {"event_id": body["event_id"], "accepted": True,
                "stored": True, "status": "accepted", "action": "stored",
                "handle": "m:12345678"}

client = AckClient()
pending_before = sp.pending()
counters_before = sp.counters()
sp.start(client)
deadline = time.monotonic() + 20.0
while sp.pending() and time.monotonic() < deadline:
    time.sleep(0.05)
result = {"pending_before": pending_before, "pending_after": sp.pending(),
          "seen": client.seen, "counters": sp.counters()}
sp.close()
with open(out, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(result, sort_keys=True))
"""


def test_process_kill_mid_flight_recovers(tmp_path):
    """Real OS-process kill: enqueue + claim, then os._exit with no cleanup.

    A fresh interpreter must see the same ids/payload bytes/counters and
    deliver everything with ACKs. (The older same-process reopen test only
    proves abandoned-claim recovery.)
    """
    root = str(tmp_path / "kill")
    manifest = str(tmp_path / "manifest.json")
    out = str(tmp_path / "result.json")
    proc_a = _run_native(_KILL_MID_FLIGHT_A, SRC_DIR, root, manifest)
    assert proc_a.returncode == 17, proc_a.stderr[-2000:]
    expected = json.loads(_pl.Path(manifest).read_text(encoding="utf-8"))
    assert len(expected["envelopes"]) == 3
    proc_b = _run_native(_RECOVER_AND_DRAIN_B, SRC_DIR, root, manifest, out)
    assert proc_b.returncode == 0, proc_b.stderr[-2000:]
    result = json.loads(_pl.Path(out).read_text(encoding="utf-8"))
    assert result["pending_before"] == 3
    assert result["pending_after"] == 0
    for env in expected["envelopes"]:
        canon = json.dumps(env, ensure_ascii=False, separators=(",", ":"),
                           sort_keys=True)
        assert result["seen"].get(env["event_id"]) == canon
    delivered = sum(v["item_count"] for k, v in result["counters"].items()
                    if k.endswith("|delivered"))
    assert delivered == 3


# Phase A: one committed item, one pre-COMMIT orphan file (crash between
# file write and DB commit), and the committed item's file deleted
# (crash between retire-unlink and DELETE commit). Then abrupt exit.
_KILL_AROUND_COMMIT_A = """
import json, os, sys, time, uuid
sys.path.insert(0, sys.argv[1])
from substrate.spool import Spool
root, manifest = sys.argv[2], sys.argv[3]
sp = Spool(root, max_items=100)
control = {
    "schema_version": 3, "contract_version": 1,
    "event_id": str(uuid.uuid4()), "kind": "capture_turn",
    "session_id": "session-1", "offset": {"start": 0, "end": 1},
    "capture_origin": "live", "batch_id": "",
    "speaker": {"id": "user", "role": "owner", "display": ""},
    "created_at": "2026-09-04T00:00:00Z",
    "payload": {"turn_id": "turn-0", "messages": [
        {"index": 0, "role": "user", "content": "control"}]},
}
sp.enqueue(control, priority=1, kind="capture_turn", capture_origin="live")
orphan_env = dict(control)
orphan_env["event_id"] = str(uuid.uuid4())
orphan_env["capture_origin"] = "history_replay"
orphan_env["payload"] = {"turn_id": "turn-9", "messages": [
    {"index": 9, "role": "user", "content": "orphan"}]}
item_id = uuid.uuid4().hex
created_at = time.time()
name = f"{int(created_at * 1000000000):020d}-{item_id}.json"
wrapper = {"item_id": item_id, "priority": 3, "kind": "capture_turn",
           "capture_origin": "history_replay",
           "event_id": orphan_env["event_id"],
           "byte_count": 0, "created_at": created_at, "envelope": orphan_env}
raw = (json.dumps(wrapper, ensure_ascii=False, separators=(",", ":"),
                  sort_keys=True) + chr(10)).encode()
tmp = os.path.join(root, f".{name}.{os.getpid()}.tmp")
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as fh:
    fh.write(raw)
    fh.flush()
    os.fsync(fh.fileno())
os.chmod(tmp, 0o600)
os.replace(tmp, os.path.join(root, name))
os.chmod(os.path.join(root, name), 0o600)
dirfd = os.open(root, os.O_RDONLY)
try:
    os.fsync(dirfd)
finally:
    os.close(dirfd)
# Post-COMMIT file loss: remove every other item file (the control row's).
for entry in os.listdir(root):
    if entry.endswith(".json") and entry != name:
        os.unlink(os.path.join(root, entry))
dirfd = os.open(root, os.O_RDONLY)
try:
    os.fsync(dirfd)
finally:
    os.close(dirfd)
data = {"control": control, "orphan": orphan_env}
with open(manifest, "w", encoding="utf-8") as fh:
    fh.write(json.dumps(data, sort_keys=True))
    fh.flush()
    os.fsync(fh.fileno())
os._exit(17)
"""


def test_process_kill_around_commit_recovers(tmp_path):
    """Pre-COMMIT orphan file and post-COMMIT file loss across a real kill.

    A fresh interpreter must adopt the orphan, rewrite the lost file from
    the payload BLOB, and deliver both items with byte-identical payloads.
    """
    root = str(tmp_path / "kill-commit")
    manifest = str(tmp_path / "manifest.json")
    out = str(tmp_path / "result.json")
    proc_a = _run_native(_KILL_AROUND_COMMIT_A, SRC_DIR, root, manifest)
    assert proc_a.returncode == 17, proc_a.stderr[-2000:]
    expected = json.loads(_pl.Path(manifest).read_text(encoding="utf-8"))
    proc_b = _run_native(_RECOVER_AND_DRAIN_B, SRC_DIR, root, manifest, out)
    assert proc_b.returncode == 0, proc_b.stderr[-2000:]
    result = json.loads(_pl.Path(out).read_text(encoding="utf-8"))
    assert result["pending_before"] == 2
    assert result["pending_after"] == 0
    wanted = {"control": expected["control"], "orphan": expected["orphan"]}
    assert set(result["seen"]) == {e["event_id"] for e in wanted.values()}
    for env in wanted.values():
        canon = json.dumps(env, ensure_ascii=False, separators=(",", ":"),
                           sort_keys=True)
        assert result["seen"][env["event_id"]] == canon
    by_outcome = {}
    for key, value in result["counters"].items():
        by_outcome[key.rsplit("|", 1)[-1]] = (
            by_outcome.get(key.rsplit("|", 1)[-1], 0) + value["item_count"])
    assert by_outcome.get("delivered") == 2
    # One enqueue counted at insert, one at orphan adoption.
    assert by_outcome.get("enqueued") == 2

def test_get_spool_singleton_start_idempotent_and_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBSTRATE_SPOOL_DIR", str(tmp_path / "singleton"))
    reset_spool()
    try:
        first = get_spool()
        assert get_spool() is first
        env = make_envelope()
        first.enqueue(env, priority=PRIORITY_LIVE,
                      kind="capture_turn", capture_origin="live")
        client = FakeClient([])
        first.start(client)
        thread = first._thread
        first.start(client)
        assert first._thread is thread
        assert drain(first) == 0
        first.stop(timeout=5.0)
        assert not thread.is_alive()
    finally:
        reset_spool()
        monkeypatch.delenv("SUBSTRATE_SPOOL_DIR", raising=False)


def test_stop_respects_deadline_on_blocked_sender(tmp_path):
    sp = new_spool(tmp_path)
    started = threading.Event()

    class BlockingClient:
        def post_json(self, path, body, **kwargs):
            started.set()
            time.sleep(30.0)
            return ack_for(body["event_id"])

    try:
        sp.enqueue(make_envelope(), priority=PRIORITY_LIVE,
                   kind="capture_turn", capture_origin="live")
        sp.start(BlockingClient())
        assert started.wait(5.0)
        before = time.monotonic()
        sp.stop(timeout=5.0)
        # The 5 s deadline fires instead of waiting out the 30 s POST.
        assert time.monotonic() - before < 6.0
    finally:
        sp.close()


def test_client_tls_redirect_and_retry_after():
    with pytest.raises(ClientError) as info:
        SubstrateClient("http://example.com", "k")
    assert info.value.category == "invalid_config"
    loopback = SubstrateClient("http://127.0.0.1:11434", "k")
    assert loopback.api_url == "http://127.0.0.1:11434"

    class RedirectResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return "https://evil.example/api/v1/ledger/events"

        def read(self, size=-1):
            return b"{}"

    import urllib.request as urlrequest

    def fake_urlopen(request, timeout):
        return RedirectResponse()

    real = urlrequest.urlopen
    urlrequest.urlopen = fake_urlopen
    try:
        client = SubstrateClient("https://memory.example", "k")
        with pytest.raises(ClientError) as redirect_info:
            client.post_json("/api/v1/ledger/events", {"a": 1}, timeout=1.0)
        assert redirect_info.value.category == "transport_error"
    finally:
        urlrequest.urlopen = real

    not_found = urllib.error.HTTPError(
        "https://memory.example/api/v1/ledger/events", 404, "Not Found",
        {}, None,
    )
    urlrequest.urlopen = lambda request, timeout: (_ for _ in ()).throw(not_found)
    try:
        client = SubstrateClient("https://memory.example", "k")
        with pytest.raises(ClientError) as not_found_info:
            client.post_json("/api/v1/ledger/events", {"a": 1}, timeout=1.0)
        assert not_found_info.value.category == "transport_error"
        assert not_found_info.value.status == 404
        assert not_found_info.value.transient is False
    finally:
        urlrequest.urlopen = real

    for code in (429, 503):
        headers = {"Retry-After": "600"}
        http_error = urllib.error.HTTPError(
            "https://memory.example/api/v1/ledger/events", code, "Slow",
            headers, None,
        )
        urlrequest.urlopen = lambda request, timeout: (_ for _ in ()).throw(http_error)
        try:
            client = SubstrateClient("https://memory.example", "k")
            with pytest.raises(ClientError) as slow_info:
                client.post_json("/api/v1/ledger/events", {"a": 1}, timeout=1.0)
            assert slow_info.value.retry_after == 600.0
            assert slow_info.value.transient is True
        finally:
            urlrequest.urlopen = real

    headers = {"Retry-After": "120"}
    http_error = urllib.error.HTTPError(
        "https://memory.example/api/v1/ledger/events", 429, "Too Many",
        headers, None,
    )
    urlrequest.urlopen = lambda request, timeout: (_ for _ in ()).throw(http_error)
    try:
        client = SubstrateClient("https://memory.example", "k")
        with pytest.raises(ClientError) as rate_info:
            client.post_json("/api/v1/ledger/events", {"a": 1}, timeout=1.0)
        assert rate_info.value.category == "rate_limited"
        assert rate_info.value.retry_after == 120.0
        assert rate_info.value.transient is True
    finally:
        urlrequest.urlopen = real
