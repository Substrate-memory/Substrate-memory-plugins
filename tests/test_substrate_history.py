"""Past-conversation import for Hermes: state.db reader, offer, tool, job, CLI.

Fixtures are real Hermes ``state.db`` layouts: the ``schema_version``,
``sessions`` and ``messages`` tables are copied from hermes-agent's
``SCHEMA_SQL`` for schemas 4, 22, 26 and 30 (see
``fixtures/hermes-state-schemas.json``). The network side is the offline
``fake_mcp`` stub from conftest; nothing touches a live server or ~/.hermes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from substrate import contract, history, plugin
from substrate.spool import PRIORITY_LIVE, PRIORITY_REPLAY

from redaction_fixture import load as load_redaction_fixture  # noqa: E402
ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "plugins" / "substrate-hermes" / "onboard.py"
SCHEMAS = json.loads((ROOT / "tests" / "fixtures" / "hermes-state-schemas.json").read_text())["schemas"]
REDACTION = load_redaction_fixture()
VERSIONS = sorted(int(v) for v in SCHEMAS)
DAY = 86_400.0
T0 = 1_772_496_000.0  # 2026-03-03T00:00:00Z


# ---------------------------------------------------------------------------
# state.db builder
# ---------------------------------------------------------------------------


def make_db(home: Path, version: int = 26) -> sqlite3.Connection:
    path = Path(home) / "state.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA synchronous=OFF")
    for sql in SCHEMAS[str(version)]:
        connection.executescript(sql)
    connection.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    connection.commit()
    return connection


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def add_session(connection, sid, *, source="telegram", started=T0, messages=(), **extra):
    columns = _columns(connection, "sessions")
    row = {"id": sid, "source": source, "started_at": started, **extra}
    row = {key: value for key, value in row.items() if key in columns}
    connection.execute(
        f"INSERT INTO sessions ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )
    message_columns = _columns(connection, "messages")
    for n, message in enumerate(messages):
        if isinstance(message, tuple):
            message = {"role": message[0], "content": message[1]}
        values = {"session_id": sid, "timestamp": started + n, **message}
        values = {key: value for key, value in values.items() if key in message_columns}
        connection.execute(
            f"INSERT INTO messages ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
            list(values.values()),
        )
    connection.commit()


def chat(n_turns: int, prefix: str = "q") -> list[tuple[str, str]]:
    out = []
    for i in range(n_turns):
        out += [("user", f"{prefix} question {i}"), ("assistant", f"{prefix} answer {i}")]
    return out


def standard_history(home: Path, version: int = 26, now: float | None = None) -> sqlite3.Connection:
    """Two human chats, automated and excluded sessions, and a live one."""
    now = time.time() if now is None else now
    db = make_db(home, version)
    add_session(db, "tg-1", source="telegram", started=T0, chat_type="dm", ended_at=T0 + 50,
                title="Trip plans", messages=chat(3, "tg"))
    add_session(db, "cli-1", source="cli", started=T0 + 2 * DAY, ended_at=T0 + 2 * DAY + 50,
                messages=chat(2, "cli"))
    add_session(db, "cron-1", source="cron", started=T0 + 3 * DAY, messages=chat(1, "cron"))
    add_session(db, "batch-1", source="batch", started=T0 + 3 * DAY + 1, messages=chat(1, "b"))
    add_session(db, "sub-1", source="subagent", started=T0 + 3 * DAY + 2,
                parent_session_id="tg-1", messages=chat(1, "sub"))
    add_session(db, "test-1", source="test", started=T0 + 3 * DAY + 3, messages=chat(1, "t"))
    add_session(db, "hook-1", source="webhook", started=T0 + 3 * DAY + 4, messages=chat(1, "w"))
    add_session(db, "empty-1", source="telegram", started=T0 + 4 * DAY)
    add_session(db, "live-1", source="telegram", started=now - 600, chat_type="dm",
                messages=chat(1, "live"))
    if "chat_type" in _columns(db, "sessions"):
        add_session(db, "grp-1", source="telegram", started=T0 + 5 * DAY, chat_type="group",
                    ended_at=T0 + 5 * DAY + 9, messages=chat(1, "grp"))
    if "hidden" in _columns(db, "sessions"):
        add_session(db, "hid-1", source="cli", started=T0 + 6 * DAY, hidden=1,
                    ended_at=T0 + 6 * DAY + 9, messages=chat(1, "hid"))
    return db


def envelopes_for(home: Path, sid: str, **kwargs) -> list[dict]:
    db = history.StateDB(history.state_db_path(home))
    row = next(r for r in db.iter_sessions() if r["id"] == sid)
    return [env for env, _ in history.iter_session_envelopes(db, row, batch_id="ab" * 8, **kwargs)]


def item_texts(items: list[dict]) -> list[str]:
    return [m["content"] for item in items if item["kind"] == "capture_turn"
            for m in item["payload"]["messages"]]


def import_calls(server) -> list[dict]:
    return [item for call in server.calls if call["tool"] == "memory_import"
            for item in call["arguments"]["items"]]


@pytest.fixture
def home(_isolated_hermes_home):
    return Path(os.environ["HERMES_HOME"])


@pytest.fixture
def connected(monkeypatch, fake_mcp):
    monkeypatch.setenv("SUBSTRATE_API_URL", f"http://127.0.0.1:{fake_mcp.server_port}")
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    return fake_mcp


def run_job(home, real_spool, client=None, **kwargs):
    from substrate.client import SubstrateClient

    client = client if client is not None else SubstrateClient.from_env()
    return history.ImportJob(home, spool=real_spool, client=client, poll_seconds=0.02,
                             drain_timeout=30.0, **kwargs).run()


# ---------------------------------------------------------------------------
# Reader and selection across schema versions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", VERSIONS)
def test_preview_selects_human_conversations_on_every_schema(home, version):
    standard_history(home, version)
    view = history.preview(home, exclude={"never-seen"}, limit=10)
    assert view["sessions"] == 2  # tg-1 and cli-1 only
    assert view["messages"] == 10
    assert view["first_day"] == "March 3, 2026" and view["last_day"] == "March 5, 2026"
    excluded = view["excluded"]
    assert excluded["automated"] == 5  # cron, batch, subagent, test, webhook
    assert excluded["current"] == 1  # live-1: open and active a few minutes ago
    has_chat_type = version >= 22
    assert excluded["group"] == (1 if has_chat_type else 0)
    assert excluded["hidden"] == (1 if version >= 26 else 0)
    assert [entry["session_id"] for entry in view["recent"]] == ["cli-1", "tg-1"]
    assert history.StateDB(history.state_db_path(home)).schema_version() == version
    assert "2 earlier Hermes conversations (10 messages, March 3, 2026 to March 5, 2026)" in \
        history.offer_question(view)


@pytest.mark.parametrize("version", VERSIONS)
def test_envelopes_are_valid_history_replay_with_stable_ids(home, version):
    standard_history(home, version)
    first = envelopes_for(home, "tg-1")
    again = envelopes_for(home, "tg-1")
    assert [e["event_id"] for e in first] == [e["event_id"] for e in again]
    turns = [e for e in first if e["kind"] == "capture_turn"]
    assert [e["offset"] for e in turns] == [
        {"start": 0, "end": 2}, {"start": 2, "end": 4}, {"start": 4, "end": 6}]
    assert first[-1]["kind"] == "capture_session"
    assert first[-1]["payload"] == {
        "boundary": "end", "session_complete": True, "message_high_water": 6,
        "platform": "telegram", "chat_type": "dm" if version >= 22 else "direct"}
    for envelope in first:
        contract.validate_envelope(envelope, idempotency_key=envelope["event_id"])
        assert envelope["capture_origin"] == "history_replay"
        assert envelope["batch_id"] == "ab" * 8
        assert envelope["speaker"] == {"id": "user", "role": "owner", "display": ""}
        assert envelope["event_id"] == contract.deterministic_event_id(
            envelope["kind"], envelope["session_id"], envelope["offset"], envelope["payload"])
    assert turns[0]["payload"]["messages"][0]["timestamp"] == "2026-03-03T00:00:00.000000Z"
    assert turns[0]["created_at"] == "2026-03-03T00:00:00.000000Z"


def test_rewound_superseded_summary_and_hidden_rows_are_left_out(home):
    db = make_db(home, 30)
    add_session(db, "s", started=T0, messages=[
        {"role": "user", "content": "kept original", "active": 0, "compacted": 1},
        {"role": "assistant", "content": "kept answer", "active": 0, "compacted": 1},
        {"role": "user", "content": "rewound question", "active": 0, "compacted": 0},
        {"role": "assistant", "content": "[summary of earlier turns]", "_compressed_summary": 1},
        {"role": "user", "content": "(placeholder)", "display_kind": "hidden"},
        {"role": "session_meta", "content": "meta"},
        {"role": "user", "content": "live question"},
        {"role": "assistant", "content": "live answer"},
    ])
    texts = item_texts(envelopes_for(home, "s"))
    assert texts == ["kept original", "kept answer", "live question", "live answer"]


def test_tool_calls_and_results_are_bounded_and_redacted(home):
    db = make_db(home, 26)
    secret_args = json.dumps({"command": "curl -H 'Authorization: Bearer sk_live_abcdefgh123'",
                              "api_key": "sk_live_abcdefgh123", "path": "/tmp/x"})
    add_session(db, "s", started=T0, messages=[
        {"role": "user", "content": "my password = hunter2, deploy it"},
        {"role": "assistant", "content": "", "tool_calls": json.dumps([{
            "id": "call-1", "type": "function",
            "function": {"name": "terminal", "arguments": secret_args}}])},
        {"role": "tool", "tool_call_id": "call-1", "tool_name": "terminal",
         "content": "token=abc123 " + "x" * 20_000},
        {"role": "assistant", "content": "Done. Key sk_live_1234567890abcdef rotated."},
    ])
    (turn, _end) = envelopes_for(home, "s")
    messages = turn["payload"]["messages"]
    assert messages[0]["content"] == "my password = [REDACTED], deploy it"
    call = messages[1]["tool_calls"][0]
    assert call["tool_name"] == "terminal"
    assert call["args"]["api_key"] == "[REDACTED]"
    assert "sk_live" not in json.dumps(call)
    tool = messages[2]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call-1"
    assert tool["content"].startswith("token=[REDACTED] ")
    assert len(tool["content"].encode()) <= contract.LIMITS["max_tool_result_bytes"]
    assert tool["result_truncated"] is True and len(tool["result_digest"]) == 64
    assert messages[3]["content"] == "Done. Key [REDACTED] rotated."


def test_shared_redaction_fixtures_apply_to_imported_text(home):
    db = make_db(home, 26)
    cases = REDACTION["text"]
    add_session(db, "s", started=T0, messages=[("user", case["in"]) for case in cases])
    texts = item_texts(envelopes_for(home, "s"))
    assert texts == [case["out"] for case in cases]


def test_oversized_turn_splits_into_contiguous_parts(home, monkeypatch):
    monkeypatch.setattr(history, "MAX_IMPORT_EVENT_BYTES", 40_000)
    db = make_db(home, 26)
    add_session(db, "s", started=T0, messages=[("user", "go")] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": f"{i} " + "y" * 8000}
        for i in range(12)] + [("assistant", "done")])
    parts = [e for e in envelopes_for(home, "s") if e["kind"] == "capture_turn"]
    assert len(parts) > 1
    assert parts[0]["offset"]["start"] == 0 and parts[-1]["offset"]["end"] == 14
    for left, right in zip(parts, parts[1:]):
        assert left["offset"]["end"] == right["offset"]["start"]
    assert all(len(contract.canonical_bytes(p)) <= 40_000 for p in parts)


def test_paging_reads_every_session_and_message_once(home, monkeypatch):
    monkeypatch.setattr(history, "SESSION_PAGE", 7)
    monkeypatch.setattr(history, "MESSAGE_PAGE", 5)
    db = make_db(home, 26)
    for n in range(23):  # same started_at for some: keyset must break ties by id
        add_session(db, f"s{n:02d}", source="cli", started=T0 + (n // 3), ended_at=T0 + 99,
                    messages=chat(4, f"s{n}"))
    reader = history.StateDB(history.state_db_path(home))
    ids = [row["id"] for row in reader.iter_sessions()]
    assert ids == [f"s{n:02d}" for n in range(23)]
    texts = item_texts(envelopes_for(home, "s07"))
    assert texts == [m[1] for m in chat(4, "s7")]


def test_large_history_streams_without_loading_it(home):
    db = make_db(home, 26)
    huge = "z" * (3 * 1024 * 1024)
    add_session(db, "big", source="cli", started=T0, ended_at=T0 + 9,
                messages=[("user", huge), ("assistant", huge)] * 6 + chat(400, "big"))
    for n in range(600):
        add_session(db, f"s{n}", source="telegram", started=T0 + DAY + n, ended_at=T0 + 2 * DAY,
                    messages=chat(2, f"s{n}"))
    db.close()
    assert (home / "state.db").stat().st_size > 30 * 1024 * 1024
    tracemalloc.start()
    try:
        view = history.preview(home)
        reader = history.StateDB(history.state_db_path(home))
        count = 0
        for row in reader.iter_sessions():
            for envelope, _ in history.iter_session_envelopes(reader, row, batch_id="cd" * 8):
                count += 1
                assert len(contract.canonical_bytes(envelope)) <= history.MAX_IMPORT_EVENT_BYTES
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert view["sessions"] == 601
    assert count == 406 + 601 + 600 * 2
    assert peak < 16 * 1024 * 1024, peak


def test_missing_or_foreign_database(home):
    assert history.preview(home)["sessions"] == 0  # no state.db yet
    sqlite3.connect(home / "state.db").execute("CREATE TABLE other (x)")
    with pytest.raises(history.HistoryError) as error:
        history.preview(home)
    assert error.value.category == "unsupported_schema"


# ---------------------------------------------------------------------------
# Import job: spool priority, backpressure, resume, dedupe, server high water
# ---------------------------------------------------------------------------


def test_import_job_delivers_everything_eligible(home, real_spool, connected):
    standard_history(home, 26)
    result = run_job(home, real_spool)
    assert result["state"] == "done", result
    items = import_calls(connected)
    sessions = {item["session_id"] for item in items}
    assert sessions == {"tg-1", "cli-1"}
    assert all(item["capture_origin"] == "history_replay" for item in items)
    assert {item["batch_id"] for item in items} == {history.profile_batch_id(home)}
    assert sum(item["kind"] == "capture_turn" for item in items) == 5
    assert sum(item["kind"] == "capture_session" for item in items) == 2
    assert result["result"] == {"sessions": 2, "turns": 5, "messages": 10, "stored": 5, "duplicate": 0,
                                "rejected": 0, "dropped": 0}
    assert result["message"] == ("Imported 2 past Hermes conversations. Substrate is reading them now; "
                                 "ask about them in a few minutes.")
    assert history.take_finished_announcement(home) == result["message"]
    assert history.take_finished_announcement(home) == ""


def test_rerun_only_produces_duplicates(home, real_spool, connected):
    standard_history(home, 26)
    seen: set[str] = set()

    def dedupe(index, item):
        if item["event_id"] in seen:
            return "duplicate"
        seen.add(item["event_id"])
        return "stored"

    connected.import_mode = dedupe
    first = run_job(home, real_spool)
    first_ids = [i["event_id"] for i in import_calls(connected)]
    connected.calls.clear()
    second = run_job(home, real_spool)
    second_ids = [i["event_id"] for i in import_calls(connected)]
    assert sorted(first_ids) == sorted(second_ids)
    assert first["result"]["stored"] == 5
    assert second["result"] == {"sessions": 2, "turns": 5, "messages": 10, "stored": 0, "duplicate": 5,
                                "rejected": 0, "dropped": 0}
    assert "already in Substrate, so nothing new was added" in second["message"]


def test_turns_substrate_already_holds_are_skipped(home, real_spool, connected):
    standard_history(home, 26)
    connected.session_status = {"tg-1": {"session_id": "tg-1", "turns_stored": 2,
                                         "message_high_water": 4, "complete": False,
                                         "last_event_at": None}}
    result = run_job(home, real_spool)
    tg = [i for i in import_calls(connected) if i["session_id"] == "tg-1"]
    assert [i["offset"] for i in tg] == [{"start": 4, "end": 6}, {"start": 6, "end": 6}]
    # The two held turns count as "already in memory", not as missing.
    assert result["result"] == {"sessions": 2, "turns": 5, "messages": 10, "stored": 3, "duplicate": 2,
                                "rejected": 0, "dropped": 0}


def test_crash_mid_import_resumes_without_gaps(home, real_spool, connected, monkeypatch):
    monkeypatch.setattr(history, "CHECKPOINT_EVERY", 3)
    db = make_db(home, 26)
    for n in range(6):
        add_session(db, f"s{n}", source="cli", started=T0 + n, ended_at=T0 + 50,
                    messages=chat(5, f"s{n}"))
    full = []
    for n in range(6):
        full += [e["event_id"] for e in envelopes_for(home, f"s{n}")]

    original = real_spool.enqueue
    calls = {"n": 0}

    def crash_after_13(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 13:
            raise OSError("disk went away")
        return original(*args, **kwargs)

    monkeypatch.setattr(real_spool, "enqueue", crash_after_13)
    with pytest.raises(OSError):
        run_job(home, real_spool)
    assert history.read_job(home)["state"] == "running"
    assert history.status(home)["state"] == "interrupted"
    assert "continues where it left off" in history.status(home)["message"]
    monkeypatch.setattr(real_spool, "enqueue", original)
    # A conversation started after the user said yes is live, not history.
    add_session(db, "newer", source="cli", started=time.time() + 5, ended_at=time.time() + 9,
                messages=chat(2, "newer"))
    result = run_job(home, real_spool)
    assert result["state"] == "done"
    assert result["result"]["sessions"] == result["sessions_total"] == 6
    sent = [i["event_id"] for i in import_calls(connected)]
    assert set(sent) == set(full)  # nothing missing, nothing newer
    assert len(sent) - len(full) <= 3  # only the turns after the last checkpoint again
    batches = {i["batch_id"] for i in import_calls(connected)}
    assert len(batches) == 1  # the resumed run keeps its batch


def test_live_turns_go_before_replay_and_replay_is_windowed(home, real_spool, monkeypatch):
    monkeypatch.setattr(history, "WINDOW_ITEMS", 4)
    db = make_db(home, 26)
    add_session(db, "old", source="cli", started=T0, ended_at=T0 + 9, messages=chat(20, "old"))
    stop = threading.Event()
    job = history.ImportJob(home, spool=real_spool, client=None, stop=stop, poll_seconds=0.02)
    worker = threading.Thread(target=job.run)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while real_spool.pending_at(PRIORITY_REPLAY)[0] < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)  # no sender: the importer must wait, not overfill
        assert real_spool.pending_at(PRIORITY_REPLAY)[0] == 4
        live = plugin._capture_envelopes("now", "reply", session_id="live", messages=[])[0]
        real_spool.enqueue(live, priority=PRIORITY_LIVE, kind="capture_turn",
                           capture_origin="live")
        batch = real_spool.claim_batch()
        assert batch[0]["capture_origin"] == "live"
        assert all(b["capture_origin"] == "history_replay" for b in batch[1:])
    finally:
        stop.set()
        worker.join(timeout=10)
    assert history.read_job(home)["state"] == "running"  # stopped, resumable


def test_rejected_items_are_reported(home, real_spool, connected):
    standard_history(home, 26)
    connected.import_mode = lambda index, item: (
        "rejected" if item["kind"] == "capture_turn" and item["offset"]["start"] == 2 else "stored")
    result = run_job(home, real_spool)
    assert result["result"]["rejected"] == 2
    assert "2 items could not be saved" in result["message"]


def test_picked_sessions_only(home, real_spool, connected):
    standard_history(home, 26)
    result = run_job(home, real_spool, picked=["cli-1", "cron-1", "live-1"])
    sessions = {i["session_id"] for i in import_calls(connected)}
    assert sessions == {"cli-1", "cron-1"}  # picked by id; the live one never
    assert result["result"]["sessions"] == 2


def test_second_job_while_one_runs_does_not_start(home, real_spool):
    standard_history(home, 26)
    with history.job_lock(home) as acquired:
        assert acquired
        assert history.job_running(home)
        assert history.start_background(home, client=None) is False


# ---------------------------------------------------------------------------
# The offer in chat (pre_llm_call) and the substrate_import tool
# ---------------------------------------------------------------------------


def _turn_context(monkeypatch, block="<memory-context>\n- fact\n</memory-context>"):
    from substrate.client import SubstrateClient

    def call(self, name, arguments, **kwargs):
        if name == "memory_turn_context":
            return ({"contract_version": 2, "session_id": arguments["session_id"], "turn": 0,
                     "block": block, "handles": [], "missing_turns": 0, "brief_version": 0,
                     "latency_ms": 1, "empty_reason": ""}, block)
        return ({"contract_version": 2}, "")

    monkeypatch.setattr(SubstrateClient, "call_tool", call)
    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(plugin, "_version_notice_shown", True)


def test_offer_is_shown_once_per_connection_and_the_answer_is_remembered(
    home, monkeypatch, fake_spool
):
    standard_history(home, 26)
    _turn_context(monkeypatch)
    context = plugin.pre_llm_call("live-1", "hi", [])["context"]
    assert context.endswith("<memory-context>\n- fact\n</memory-context>")
    assert context.startswith("<substrate-import-offer>")
    assert "2 earlier Hermes conversations (10 messages, March 3, 2026 to March 5, 2026)" \
        in context
    assert "Import all, let me pick, or not now?" in context
    assert "substrate_import" in context
    assert history.was_asked(home)
    # Asked once: later turns of the connection stay quiet until the user answers.
    for _ in range(2):
        assert plugin.pre_llm_call("live-1", "hi", [])["context"] == \
            "<memory-context>\n- fact\n</memory-context>"
    assert json.loads(plugin.substrate_import({"action": "decline"}))["status"] == "declined"
    assert history.read_decision(home) == "no"
    assert history.was_asked(home)  # the decline keeps the asked record
    plugin._PREVIEW_CACHE.clear()  # a new process remembers too
    assert plugin.pre_llm_call("live-1", "hi", [])["context"] == \
        "<memory-context>\n- fact\n</memory-context>"


def test_unanswered_offer_is_not_repeated_but_a_direct_request_still_imports(
    home, monkeypatch, real_spool, connected
):
    from substrate.client import SubstrateClient

    standard_history(home, 26)
    real_call_tool = SubstrateClient.call_tool
    _turn_context(monkeypatch)
    assert "substrate-import-offer" in plugin.pre_llm_call("live-1", "hi", [])["context"]
    plugin._PREVIEW_CACHE.clear()
    assert "substrate-import-offer" not in plugin.pre_llm_call("live-1", "hi", [])["context"]
    # The turn-context stub fakes every MCP tool; the import needs the real client.
    monkeypatch.setattr(SubstrateClient, "call_tool", real_call_tool)
    started = json.loads(plugin.substrate_import({"action": "start"}))
    assert started["status"] == "started"
    assert history.read_decision(home) == "yes"
    # Let the background import finish inside this test, so it does not hold the
    # job lock into the next test.
    deadline = time.monotonic() + 60
    while history.status(home)["state"] != "done" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert history.status(home)["state"] == "done", history.status(home)


def test_offer_comes_with_an_empty_memory_block_too(home, monkeypatch, fake_spool):
    standard_history(home, 26)
    _turn_context(monkeypatch, block="")
    context = plugin.pre_llm_call("live-1", "hi", [])["context"]
    assert context.startswith("<substrate-import-offer>")


def test_no_history_records_none_and_never_offers(home, monkeypatch, fake_spool):
    db = make_db(home, 26)
    add_session(db, "live-1", started=time.time() - 60, messages=chat(1))  # only the live one
    add_session(db, "cron-1", source="cron", started=T0, messages=chat(1))
    _turn_context(monkeypatch)
    assert plugin.pre_llm_call("live-1", "hi", [])["context"] == \
        "<memory-context>\n- fact\n</memory-context>"
    assert history.read_decision(home) == "none"


def test_no_offer_before_connection_or_on_rejected_key(home, monkeypatch, fake_spool):
    standard_history(home, 26)
    monkeypatch.setattr(plugin, "_version_notice_shown", True)
    monkeypatch.setattr(plugin, "_start_login", lambda: {
        "status": "authorization_pending", "verification_uri_complete": "https://x/oauth/device",
        "user_code": "BCDF-GHJK", "expires_in": 600})
    context = plugin.pre_llm_call("live-1", "hi", [])["context"]
    assert "substrate-import-offer" not in context
    from substrate.client import ClientError, SubstrateClient

    monkeypatch.setenv("SUBSTRATE_API_KEY", "k")
    monkeypatch.setattr(SubstrateClient, "call_tool", lambda *a, **k: (_ for _ in ()).throw(
        ClientError("unauthorized", status=401)))
    assert plugin.pre_llm_call("live-1", "hi", []) is None
    assert history.read_decision(home) == ""


def test_unreadable_history_asks_later_without_recording(home, monkeypatch, fake_spool):
    (home / "state.db").write_bytes(b"not a database" * 100)
    _turn_context(monkeypatch)
    assert plugin.pre_llm_call("s", "hi", [])["context"] == \
        "<memory-context>\n- fact\n</memory-context>"
    assert history.read_decision(home) == ""


def test_tool_preview_start_status_and_finished_announcement(home, monkeypatch, real_spool,
                                                             connected):
    standard_history(home, 26)
    plugin._note_live_session("live-1")
    preview = json.loads(plugin.substrate_import({"action": "preview", "limit": 5}))
    assert preview["sessions"] == 2
    assert [s["session_id"] for s in preview["recent"]] == ["cli-1", "tg-1"]
    assert preview["recent"][1]["title"] == "Trip plans"
    assert preview["question"].endswith("Import all, let me pick, or not now?")
    started = json.loads(plugin.substrate_import({"action": "start"}))
    assert started["status"] == "started" and started["sessions"] == 2
    assert history.read_decision(home) == "yes"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = json.loads(plugin.substrate_import({"action": "status"}))
        if state["state"] == "done":
            break
        time.sleep(0.05)
    assert state["state"] == "done", state
    assert state["message"].startswith("Imported 2 past Hermes conversations.")
    _turn_context(monkeypatch)
    context = plugin.pre_llm_call("live-1", "hi", [])["context"]
    assert "<substrate-import>" in context and "Imported 2 past Hermes conversations" in context
    assert "substrate-import-offer" not in context
    again = plugin.pre_llm_call("live-1", "hi", [])["context"]
    assert "<substrate-import>" not in again


def test_tool_start_with_picked_sessions_records_picked(home, real_spool, connected):
    standard_history(home, 26)
    started = json.loads(plugin.substrate_import({"action": "start", "session_ids": ["tg-1"]}))
    assert started["sessions"] == 1
    assert history.read_decision(home) == "picked"
    deadline = time.monotonic() + 60
    while history.status(home)["state"] != "done" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert {i["session_id"] for i in import_calls(connected)} == {"tg-1"}


def test_tool_rejects_bad_arguments_and_needs_a_connection(home, monkeypatch):
    for bad in ({}, {"action": "go"}, {"action": "start", "session_ids": []},
                {"action": "start", "session_ids": [1]}, {"action": "preview", "limit": 500},
                {"action": "status", "extra": 1}):
        assert json.loads(plugin.substrate_import(bad)) == {"error": "invalid_request"}
    standard_history(home, 26)
    monkeypatch.setattr(plugin, "_start_login", lambda: None)
    result = json.loads(plugin.substrate_import({"action": "start"}))
    assert result["status"] == "authorization_required"
    assert history.read_decision(home) == ""


def test_tool_start_with_nothing_to_import(home, connected):
    make_db(home, 26)
    result = json.loads(plugin.substrate_import({"action": "start"}))
    assert result["status"] == "nothing_to_import"
    assert history.read_decision(home) == "none"


def test_interrupted_import_resumes_on_the_next_turn(home, monkeypatch, real_spool, connected):
    standard_history(home, 26)
    history.record_decision(home, "yes")
    history._save_job(home, {"version": 1, "state": "running", "batch_id": "ef" * 8,
                             "picked": None, "exclude": [], "cursor": None, "next_index": 0,
                             "sessions_total": 2, "sessions_done": 0, "turns_queued": 0,
                             "base": history._replay_counters(real_spool)})
    plugin.pre_llm_call("live-1", "hi", [])
    deadline = time.monotonic() + 60
    while history.status(home)["state"] != "done" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert history.status(home)["state"] == "done", history.status(home)
    assert {i["batch_id"] for i in import_calls(connected)} == {"ef" * 8}


# ---------------------------------------------------------------------------
# CLI: onboard.py import
# ---------------------------------------------------------------------------


def _cli(home: Path, *args: str, origin: str = "", key: str = "", timeout: float = 60,
         **env_extra: str):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SUBSTRATE_", "PYTHON")) and k not in {"HERMES_HOME", "VIRTUAL_ENV"}}
    env.update({"HERMES_HOME": str(home), "HOME": str(home), **env_extra})
    if origin:
        env["SUBSTRATE_API_URL"] = origin
    if key:
        env["SUBSTRATE_API_KEY"] = key
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def test_cli_preview_list_no_and_status(home):
    standard_history(home, 26)
    shown = _cli(home, "import")
    assert shown.returncode == 0, shown.stderr
    assert "Import all, let me pick, or not now?" in shown.stdout
    assert f"python {CLI} import --yes" in shown.stdout
    listed = _cli(home, "import", "--list", "--json")
    payload = json.loads(listed.stdout)
    assert [s["session_id"] for s in payload["recent"]] == ["cli-1", "tg-1"]
    status = _cli(home, "import", "--status")
    assert "No import has run" in status.stdout
    declined = _cli(home, "import", "--no")
    assert declined.returncode == 0 and "not asked again" in declined.stdout
    assert history.read_decision(home) == "no"


def test_cli_excludes_the_session_running_it(home):
    standard_history(home, 26)
    payload = json.loads(_cli(home, "import", "--json", HERMES_SESSION_ID="tg-1").stdout)
    assert payload["sessions"] == 1 and payload["excluded"]["current"] == 2


def test_cli_yes_imports_in_the_background(home, fake_mcp):
    standard_history(home, 26)
    origin = f"http://127.0.0.1:{fake_mcp.server_port}"
    started = _cli(home, "import", "--yes", origin=origin, key="k")
    assert started.returncode == 0, started.stdout + started.stderr
    assert started.stdout.startswith("Started importing 2 earlier Hermes conversations")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = json.loads(_cli(home, "import", "--status", "--json").stdout)
        if state["state"] == "done":
            break
        time.sleep(0.2)
    assert state["state"] == "done", state
    assert state["result"]["turns"] == 5
    assert {i["session_id"] for i in import_calls(fake_mcp)} == {"tg-1", "cli-1"}
    assert history.read_decision(home) == "yes"


def test_cli_yes_without_connection_says_connect_first(home):
    standard_history(home, 26)
    result = _cli(home, "import", "--yes")
    assert result.returncode == 1
    assert "not connected yet" in result.stdout and "start" in result.stdout
    assert history.read_decision(home) == ""


def test_cli_run_foreground_with_picked_session(home, fake_mcp):
    standard_history(home, 26)
    origin = f"http://127.0.0.1:{fake_mcp.server_port}"
    result = _cli(home, "import", "--run", "--session", "cli-1", "--json", origin=origin, key="k")
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["result"]["sessions"] == 1
    assert history.read_decision(home) == "picked"


def test_import_stops_resumably_when_substrate_accepts_nothing(home, real_spool, monkeypatch):
    """Offline or a revoked key: the job does not wait forever; it stops with
    its checkpoint and continues on the next message or start."""
    monkeypatch.setattr(history, "WINDOW_ITEMS", 3)
    db = make_db(home, 26)
    add_session(db, "old", source="cli", started=T0, ended_at=T0 + 9, messages=chat(10, "old"))
    job = history.ImportJob(home, spool=real_spool, client=None, poll_seconds=0.02,
                            stall_seconds=0.3)
    started = time.monotonic()
    result = job.run()
    assert time.monotonic() - started < 10
    assert job.stalled
    assert result["state"] == "interrupted"
    assert history.read_job(home)["state"] == "running"


def test_long_turn_over_128_messages_splits_and_resumes_part_by_part(home, monkeypatch):
    """A turn the server would truncate (>128 messages) is split; a crash after
    part 1 resumes at part 2, not after the whole turn."""
    db = make_db(home, 26)
    messages = [("user", "go")] + [("assistant", f"step {i}") for i in range(199)]
    add_session(db, "long", started=T0, messages=messages)
    row = next(history.StateDB(history.state_db_path(home)).iter_sessions(None))
    everything = list(history.iter_session_envelopes(
        history.StateDB(history.state_db_path(home)), row, batch_id="b" * 8))
    turns = [(env, resume) for env, resume in everything if env["kind"] == "capture_turn"]
    assert len(turns) >= 2
    assert all(len(env["payload"]["messages"]) <= contract.MAX_PAYLOAD_ITEMS for env, _ in turns)
    assert turns[0][0]["offset"]["start"] == 0 and turns[-1][0]["offset"]["end"] == 200
    # Resume after part 1: the next part is still yielded.
    first_resume = turns[0][1]
    resumed = [
        env for env, _ in history.iter_session_envelopes(
            history.StateDB(history.state_db_path(home)), row, batch_id="b" * 8,
            start_index=first_resume)
        if env["kind"] == "capture_turn"
    ]
    assert [e["event_id"] for e in resumed] == [e["event_id"] for e, _ in turns[1:]]
