"""Import coverage: every local transcript location the importers read (substrate_sync.py).

Codex: sessions/, archived_sessions/, the rollouts that state_*.sqlite thread catalogues
list (inside the Codex home only), and on Windows the same under running WSL distros.
Claude Code: the projects folder (CLAUDE_CONFIG_DIR honoured) and the retention line.
Every fixture is local; the upload test uses the loopback fake MCP server from
test_substrate_import_offer.py. Nothing reaches the network or a real home directory.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_substrate_import_offer import FakeMcp, ticket_args, ticket_env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CODEX_SCRIPT = REPO / "plugins" / "substrate-codex" / "scripts" / "substrate_sync.py"
CLAUDE_SCRIPT = REPO / "plugins" / "substrate-claude" / "scripts" / "substrate_sync.py"
TS = "2026-09-01T10:00:00.000Z"
NOTE = "Cloud-only chats aren't on this computer."


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("substrate_sync_cov_%d" % id(path), str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


sync = _load(CODEX_SCRIPT)
_ORIGINAL_RUNNING_DISTROS = sync._wsl_running_distros  # before any fixture patches it


@pytest.fixture()
def server():
    fake = FakeMcp()
    yield fake
    fake.close()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    data = tmp_path / "data"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("SUBSTRATE_DATA_DIR", str(data))
    monkeypatch.setenv("SUBSTRATE_SYNC_BACKOFF_MAX", "0")
    monkeypatch.delenv("SUBSTRATE_MCP_URL", raising=False)
    monkeypatch.setattr(sync, "_sleep", lambda seconds: None)
    # Off Windows by default: no wsl.exe, no UNC shares. WSL tests opt in explicitly.
    monkeypatch.setattr(sync, "WINDOWS", False)
    monkeypatch.setattr(sync, "_wsl_running_distros", lambda: [])
    return SimpleNamespace(claude=claude, codex=codex, data=data, tmp=tmp_path)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def write_rollout(path: Path, session_id: str, turns: int = 2, *, age_days: float = 1.0,
                  text: str = "codex question {n}") -> Path:
    """A Codex rollout file with a session_meta line and user/assistant messages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "session_meta", "timestamp": TS, "payload": {"id": session_id}}]
    for n in range(turns):
        lines.append({"type": "response_item", "timestamp": TS, "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text.format(n=n)}]}})
        lines.append({"type": "response_item", "timestamp": TS, "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "codex answer %d" % n}]}})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def sessions_rollout(home: Path, session_id: str, **kwargs) -> Path:
    name = "rollout-2026-09-01T10-00-00-%s.jsonl" % session_id
    return write_rollout(home / "sessions" / "2026" / "09" / "01" / name, session_id, **kwargs)


def archived_rollout(home: Path, session_id: str, **kwargs) -> Path:
    name = "rollout-2026-08-02T09-00-00-%s.jsonl" % session_id
    return write_rollout(home / "archived_sessions" / name, session_id, **kwargs)


def make_state(path: Path, rows, *, title: str = "SECRET-TITLE-XYZ", cwd: str = "/secret/cwd") -> Path:
    """A minimal Codex state database: threads(id, rollout_path, title, cwd, archived)."""
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, "
                       "title TEXT NOT NULL, cwd TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0)")
    connection.executemany(
        "INSERT INTO threads (id, rollout_path, title, cwd, archived) VALUES (?, ?, ?, ?, ?)",
        [(sid, str(p), title, cwd, archived) for sid, p, archived in rows])
    connection.commit()
    connection.close()
    return path


def run_preview(capsys, *args: str) -> dict:
    code = sync.main(["--host", "codex", "--preview", *args])
    assert code == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def catalogue_ids(host: str = "codex", data_dir: str = "") -> list:
    return [(entry["session_id"], entry["location"]) for entry in sync._catalogue(host, set(), None, data_dir)]


# ---------------------------------------------------------------------------
# Codex: archived sessions, dedupe, counts and the coverage line
# ---------------------------------------------------------------------------

def test_archived_sessions_are_listed(home, capsys) -> None:
    archived_rollout(home.codex, "arch-1")
    assert catalogue_ids() == [("arch-1", "archived")]
    preview = run_preview(capsys)
    assert preview["sessions"] == 1 and preview["locations"] == {"archived": 1}
    assert preview["coverage"] == "Found 1 conversation. " + NOTE


def test_counts_by_location_and_coverage_line(home, capsys) -> None:
    sessions_rollout(home.codex, "live-a", turns=3)
    sessions_rollout(home.codex, "live-b")
    archived_rollout(home.codex, "old-1", turns=4)
    preview = run_preview(capsys)
    assert preview["sessions"] == 3 and preview["turns"] == 9
    assert preview["locations"] == {"sessions": 2, "archived": 1}
    assert preview["coverage"] == "Found 3 conversations (sessions 2, archived 1). " + NOTE


def test_offer_sentence_names_locations_and_cloud_limit(home, capsys) -> None:
    sessions_rollout(home.codex, "live-a")
    archived_rollout(home.codex, "old-1")
    summary = sync._preview("codex", str(home.data), [])
    sentence = sync._offer_sentence(summary)
    assert sentence.startswith("I found 2 past conversations on this computer (sessions 1, archived 1; "
                               "4 turns, 1 Sep 2026 to 1 Sep 2026). " + NOTE + " Import them into "
                               "Substrate? Import all / Let me pick / Not now")


def test_same_session_in_two_places_is_imported_once(home) -> None:
    sessions_rollout(home.codex, "dup-1")
    archived_rollout(home.codex, "dup-1")
    assert catalogue_ids() == [("dup-1", "sessions")]


def test_same_rollout_listed_by_catalogue_is_not_read_twice(home) -> None:
    path = sessions_rollout(home.codex, "dup-2")
    make_state(home.codex / "state_5.sqlite", [("dup-2", path, 0)])
    sources = sync._transcript_sources("codex")
    assert [location for _path, location in sources] == ["sessions"]
    assert catalogue_ids() == [("dup-2", "sessions")]


def test_empty_copy_does_not_hide_a_real_copy(home) -> None:
    sessions_rollout(home.codex, "copy-1", turns=0)  # no messages: skipped, not counted
    archived_rollout(home.codex, "copy-1", turns=2)
    assert catalogue_ids() == [("copy-1", "archived")]


def test_setup_and_current_sessions_still_excluded_in_archived(home, capsys) -> None:
    archived_rollout(home.codex, "current-1")
    archived_rollout(home.codex, "older-1")
    proc_code = sync.main(["--host", "codex", "--preview", "--exclude-session", "current-1"])
    assert proc_code == 0
    preview = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert preview["sessions"] == 1 and preview["excluded"] == ["current-1"]


# ---------------------------------------------------------------------------
# Codex: the thread catalogue (state_*.sqlite), read-only and tolerant
# ---------------------------------------------------------------------------

def test_catalogue_lists_rollouts_outside_the_session_folders(home) -> None:
    moved = write_rollout(home.codex / "moved" / "rollout-x.jsonl", "moved-1")
    make_state(home.codex / "state_5.sqlite", [("moved-1", moved, 1)])
    assert catalogue_ids() == [("moved-1", "catalogue")]


def test_catalogue_rejects_paths_outside_the_codex_home(home, tmp_path) -> None:
    outside = write_rollout(tmp_path / "elsewhere" / "rollout-out.jsonl", "out-1")
    inside_not_jsonl = home.codex / "notes.txt"
    inside_not_jsonl.write_text("{}\n", encoding="utf-8")
    relative_escape = "../elsewhere/rollout-out.jsonl"
    make_state(home.codex / "state_5.sqlite", [
        ("out-1", outside, 0),
        ("out-2", inside_not_jsonl, 0),
        ("out-3", relative_escape, 0),
    ])
    assert sync._catalogued_rollouts(str(home.codex)) == []
    assert catalogue_ids() == []


def test_catalogue_relative_path_inside_home_is_read(home) -> None:
    write_rollout(home.codex / "moved" / "rollout-rel.jsonl", "rel-1")
    make_state(home.codex / "state_5.sqlite", [("rel-1", "moved/rollout-rel.jsonl", 0)])
    assert catalogue_ids() == [("rel-1", "catalogue")]


def test_catalogue_row_without_a_file_is_skipped(home, capsys) -> None:
    make_state(home.codex / "state_5.sqlite", [("gone-1", home.codex / "moved" / "gone.jsonl", 0)])
    sessions_rollout(home.codex, "live-1")
    preview = run_preview(capsys)
    assert preview["sessions"] == 1 and preview["locations"] == {"sessions": 1}


def test_missing_state_database_changes_nothing(home, capsys) -> None:
    sessions_rollout(home.codex, "live-1")
    assert not (home.codex / "state_5.sqlite").exists()
    preview = run_preview(capsys)
    assert preview["sessions"] == 1
    assert not (home.codex / "state_5.sqlite").exists()  # never created by reading


@pytest.mark.parametrize("junk", [b"not a sqlite database at all" * 200, b"", b"\x00\xff\xfe" * 64])
def test_corrupt_state_database_is_ignored(home, capsys, junk: bytes) -> None:
    (home.codex / "state_5.sqlite").write_bytes(junk)
    sessions_rollout(home.codex, "live-1")
    assert sync._state_rollout_paths(str(home.codex / "state_5.sqlite")) == []
    preview = run_preview(capsys)
    assert preview["sessions"] == 1 and preview["coverage"] == "Found 1 conversation. " + NOTE


def test_state_database_with_other_schema_is_ignored(home) -> None:
    connection = sqlite3.connect(str(home.codex / "state_5.sqlite"))
    connection.execute("CREATE TABLE other (x TEXT)")
    connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT)")
    connection.execute("INSERT INTO threads VALUES ('a', 'SECRET-TITLE-XYZ')")
    connection.commit()
    connection.close()
    assert sync._state_rollout_paths(str(home.codex / "state_5.sqlite")) == []


def test_every_state_version_is_read_and_repeats_are_dropped(home) -> None:
    moved_a = write_rollout(home.codex / "moved" / "rollout-a.jsonl", "a-1")
    moved_b = write_rollout(home.codex / "moved" / "rollout-b.jsonl", "b-1")
    make_state(home.codex / "state_4.sqlite", [("a-1", moved_a, 0)])
    make_state(home.codex / "state_5.sqlite", [("a-1", moved_a, 0), ("b-1", moved_b, 1)])
    assert sorted(catalogue_ids()) == [("a-1", "catalogue"), ("b-1", "catalogue")]


def test_state_database_is_opened_read_only_and_only_paths_are_read(home) -> None:
    moved = write_rollout(home.codex / "moved" / "rollout-ro.jsonl", "ro-1")
    db = make_state(home.codex / "state_5.sqlite", [("ro-1", moved, 0)])

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = digest(db)
    names = sorted(os.listdir(home.codex))
    rows = sync._state_rollout_paths(str(db))
    assert rows == [str(moved)]              # the title and cwd columns never leave SQLite
    assert "SECRET-TITLE-XYZ" not in json.dumps(rows)
    assert digest(db) == before
    assert sorted(os.listdir(home.codex)) == names  # no -journal or -wal created


# ---------------------------------------------------------------------------
# Windows and WSL: running distros only, cached, never a fork of the rules
# ---------------------------------------------------------------------------

def _wsl_share(tmp_path: Path) -> Path:
    share = tmp_path / "wsl-share"
    rollout = "rollout-2026-07-04T08-00-00-wsl-1.jsonl"
    write_rollout(share / "Ubuntu" / "home" / "pavel" / ".codex" / "sessions" / "2026" / rollout,
                  "wsl-1", turns=2)
    write_rollout(share / "Ubuntu" / "home" / "pavel" / ".codex" / "archived_sessions" /
                  "rollout-2026-06-01T08-00-00-wsl-2.jsonl", "wsl-2", turns=1)
    write_rollout(share / "Stopped" / "home" / "pavel" / ".codex" / "sessions" / "2026" /
                  "rollout-2026-07-04T08-00-00-stop-1.jsonl", "stop-1")
    return share


def test_running_wsl_distro_homes_are_read(home, tmp_path, monkeypatch, capsys) -> None:
    share = _wsl_share(tmp_path)
    monkeypatch.setattr(sync, "WINDOWS", True)
    monkeypatch.setattr(sync, "WSL_SHARES", (str(share),))
    monkeypatch.setattr(sync, "_wsl_running_distros", lambda: ["Ubuntu"])
    sessions_rollout(home.codex, "local-1")
    assert sorted(catalogue_ids(data_dir=str(home.data))) == [
        ("local-1", "sessions"), ("wsl-1", "wsl"), ("wsl-2", "wsl")]
    preview = run_preview(capsys, "--data-dir", str(home.data))
    assert preview["locations"] == {"sessions": 1, "wsl": 2}
    assert preview["coverage"] == "Found 3 conversations (sessions 1, WSL 2). " + NOTE


def test_stopped_wsl_distro_is_neither_listed_nor_started(home, tmp_path, monkeypatch) -> None:
    share = _wsl_share(tmp_path)
    monkeypatch.setattr(sync, "WINDOWS", True)
    monkeypatch.setattr(sync, "WSL_SHARES", (str(share),))
    monkeypatch.setattr(sync, "_wsl_running_distros", lambda: [])  # wsl --list --running is empty
    assert sync._wsl_codex_homes(str(home.data)) == []
    assert "stop-1" not in [sid for sid, _loc in catalogue_ids(data_dir=str(home.data))]


def test_wsl_is_never_scanned_off_windows(home, tmp_path, monkeypatch) -> None:
    share = _wsl_share(tmp_path)
    monkeypatch.setattr(sync, "WSL_SHARES", (str(share),))
    monkeypatch.setattr(sync, "_wsl_running_distros", lambda: ["Ubuntu"])
    assert sync._wsl_codex_homes(str(home.data)) == []
    assert catalogue_ids(data_dir=str(home.data)) == []


def test_wsl_scan_is_cached_in_the_data_folder(home, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sync, "WINDOWS", True)
    calls = []

    def fake_scan():
        calls.append(1)
        return [str(tmp_path / "home-a" / ".codex")]

    monkeypatch.setattr(sync, "_scan_wsl_codex_homes", fake_scan)
    first = sync._wsl_codex_homes(str(home.data))
    second = sync._wsl_codex_homes(str(home.data))
    assert first == second and len(calls) == 1          # the offer hook runs on every prompt
    cache = home.data / sync.WSL_CACHE_FILE
    saved = json.loads(cache.read_text())
    saved["at"] = time.time() - sync.WSL_CACHE_SECONDS - 60
    cache.write_text(json.dumps(saved))
    sync._wsl_codex_homes(str(home.data))
    assert len(calls) == 2                               # expired: scanned again


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", json.dumps({"at": "x", "homes": 3})])
def test_corrupt_wsl_cache_is_rescanned(home, monkeypatch, content: str) -> None:
    monkeypatch.setattr(sync, "WINDOWS", True)
    monkeypatch.setattr(sync, "_scan_wsl_codex_homes", lambda: ["/rescanned/.codex"])
    home.data.mkdir(parents=True)
    (home.data / sync.WSL_CACHE_FILE).write_text(content)
    assert sync._wsl_codex_homes(str(home.data)) == ["/rescanned/.codex"]


def test_read_only_data_folder_still_scans_wsl(home, monkeypatch) -> None:
    monkeypatch.setattr(sync, "WINDOWS", True)
    monkeypatch.setattr(sync, "_scan_wsl_codex_homes", lambda: ["/a/.codex"])
    blocked = home.tmp / "not-a-dir"
    blocked.write_text("a file, so the cache cannot be written")
    assert sync._wsl_codex_homes(str(blocked)) == ["/a/.codex"]


@pytest.mark.parametrize("raw,expected", [
    ("﻿Ubuntu\r\nDebian\r\n".encode("utf-16-le"), ["Ubuntu", "Debian"]),
    (b"Ubuntu-22.04\n", ["Ubuntu-22.04"]),
    ("There are no running distributions.\r\n".encode("utf-16-le"), []),
    ("bad/name\r\n..\r\n".encode("utf-16-le"), []),
    (b"", []),
])
def test_wsl_distro_list_parsing(raw: bytes, expected: list) -> None:
    assert sync._parse_wsl_distros(raw) == expected


def test_wsl_list_failure_is_no_distros(monkeypatch) -> None:
    monkeypatch.setattr(sync, "WINDOWS", True)
    monkeypatch.setattr(sync, "_wsl_running_distros", _ORIGINAL_RUNNING_DISTROS)

    def boom(*_args, **_kwargs):
        raise FileNotFoundError("wsl.exe")

    monkeypatch.setattr(sync.subprocess, "run", boom)
    assert sync._wsl_running_distros() == []


# ---------------------------------------------------------------------------
# Upload: archived and catalogue sessions reach the server once, with the same redaction
# ---------------------------------------------------------------------------

def test_archived_and_catalogued_sessions_upload_once(home, server, monkeypatch, capsys) -> None:
    sessions_rollout(home.codex, "live-1", turns=2)
    archived_rollout(home.codex, "old-1", turns=2)
    moved = write_rollout(home.codex / "moved" / "rollout-m.jsonl", "moved-1")
    archived_path = home.codex / "archived_sessions" / "rollout-2026-08-02T09-00-00-old-1.jsonl"
    make_state(home.codex / "state_5.sqlite", [("moved-1", moved, 1), ("old-1", archived_path, 1)])
    ticket_env(monkeypatch, server.url)
    code = sync.main(["--host", "codex", "--upload", "--all", *ticket_args()])
    assert code == 0
    assert capsys.readouterr().out.strip().startswith("Imported 3 conversations.")
    items = server.sent_items()
    seals = [i["session_id"] for i in items if i["kind"] == "capture_session"]
    assert sorted(seals) == ["live-1", "moved-1", "old-1"]          # each sealed once
    assert {i["session_id"] for i in items} == {"live-1", "moved-1", "old-1"}


def test_upload_never_sends_catalogue_titles_or_working_directories(home, server, monkeypatch) -> None:
    moved = write_rollout(home.codex / "moved" / "rollout-t.jsonl", "title-1")
    make_state(home.codex / "state_5.sqlite", [("title-1", moved, 0)], title="SECRET-TITLE-XYZ",
               cwd="/secret/cwd-QQQ")
    ticket_env(monkeypatch, server.url)
    assert sync.main(["--host", "codex", "--upload", "--all", *ticket_args()]) == 0
    sent = json.dumps(server.sent_items())
    assert "SECRET-TITLE-XYZ" not in sent and "cwd-QQQ" not in sent


# ---------------------------------------------------------------------------
# Claude Code: CLAUDE_CONFIG_DIR, the projects location, and the retention line
# ---------------------------------------------------------------------------

def claude_session(home: Path, session_id: str, *, age_days: float) -> Path:
    path = home / "projects" / "proj" / ("%s.jsonl" % session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"type": "user", "sessionId": session_id, "timestamp": TS,
            "message": {"role": "user", "content": "hello from %s" % session_id}}
    answer = {"type": "assistant", "sessionId": session_id, "timestamp": TS,
              "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
    path.write_text(json.dumps(line) + "\n" + json.dumps(answer) + "\n", encoding="utf-8")
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_claude_location_and_coverage(home, capsys) -> None:
    claude_session(home.claude, "c-1", age_days=2)
    code = sync.main(["--host", "claude", "--preview"])
    preview = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and preview["locations"] == {"projects": 1}
    assert preview["coverage"] == "Found 1 conversation. " + NOTE
    assert preview["history_note"] == ""


def test_claude_retention_line_when_oldest_is_about_thirty_days(home, capsys) -> None:
    claude_session(home.claude, "old-1", age_days=30)
    claude_session(home.claude, "newer-1", age_days=2)
    sync.main(["--host", "claude", "--preview"])
    preview = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert preview["history_note"] == ("Older Claude Code history was already removed by Claude Code "
                                       "(its retention limit is 30 days).")
    assert sync._offer_sentence(preview).endswith(
        preview["history_note"] + " Import them into Substrate? Import all / Let me pick / Not now")


def test_claude_retention_line_absent_for_recent_history(home, capsys) -> None:
    claude_session(home.claude, "fresh-1", age_days=5)
    sync.main(["--host", "claude", "--preview"])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["history_note"] == ""


def test_claude_retention_setting_is_read_not_changed(home, capsys) -> None:
    settings = home.claude / "settings.json"
    settings.write_text(json.dumps({"cleanupPeriodDays": 90, "theme": "dark"}))
    before = settings.read_text()
    claude_session(home.claude, "kept-1", age_days=30)     # 30 days is not the 90-day window
    sync.main(["--host", "claude", "--preview"])
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["history_note"] == ""
    assert settings.read_text() == before                  # never rewritten


def test_claude_retention_line_follows_an_explicit_window(home, capsys) -> None:
    (home.claude / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 90}))
    claude_session(home.claude, "long-1", age_days=89)
    sync.main(["--host", "claude", "--preview"])
    note = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["history_note"]
    assert note.endswith("(its retention limit is 90 days).")


@pytest.mark.parametrize("value", ["thirty", True, -5, 0, 1.5])
def test_bad_retention_setting_falls_back_to_default(home, value) -> None:
    (home.claude / "settings.json").write_text(json.dumps({"cleanupPeriodDays": value}))
    assert sync._claude_retention_days() == 30


def test_claude_config_dir_still_honoured_for_locations(home, tmp_path, monkeypatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    claude_session(elsewhere, "moved-cfg", age_days=1)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    assert [(e["session_id"], e["location"]) for e in sync._catalogue("claude")] == [
        ("moved-cfg", "projects")]


# ---------------------------------------------------------------------------
# The two shipped copies of the script stay byte-identical
# ---------------------------------------------------------------------------

def test_claude_and_codex_copies_are_identical() -> None:
    assert CLAUDE_SCRIPT.read_bytes() == CODEX_SCRIPT.read_bytes()


# ---------------------------------------------------------------------------
# Codex threads: a subagent rollout shares its conversation's session id. The
# conversation's own rollout must win wherever it lives (sessions or archived).
# ---------------------------------------------------------------------------

def subagent_rollout(path: Path, root_id: str, thread_id: str, turns: int) -> Path:
    """A subagent thread: its own id, the root conversation's session_id, a parent link."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "timestamp": TS, "payload": {
        "id": thread_id, "session_id": root_id, "parent_thread_id": root_id,
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": root_id}}}}}
    lines = [meta]
    for n in range(turns):
        lines.append({"type": "response_item", "timestamp": TS, "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "subagent task %d" % n}]}})
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_root_in_archive_beats_subagent_in_sessions(home) -> None:
    subagent_rollout(home.codex / "sessions" / "2026" / "09" / "02" /
                     "rollout-2026-09-02T10-00-00-thread-sub.jsonl", "root-1", "thread-sub", turns=9)
    archived_rollout(home.codex, "root-1", turns=2)
    entries = sync._catalogue("codex", set(), None, "")
    assert [(e["session_id"], e["location"], len(e["turns"])) for e in entries] == [
        ("root-1", "archived", 2)]


def test_root_beats_subagent_even_with_fewer_turns(home) -> None:
    archived_rollout(home.codex, "root-2", turns=1)
    subagent_rollout(home.codex / "sessions" / "2026" / "09" / "03" /
                     "rollout-2026-09-03T10-00-00-thread-x.jsonl", "root-2", "thread-x", turns=8)
    entries = sync._catalogue("codex", set(), None, "")
    assert [(e["session_id"], e["location"], len(e["turns"])) for e in entries] == [
        ("root-2", "archived", 1)]


def test_subagent_only_still_listed_under_its_conversation(home) -> None:
    subagent_rollout(home.codex / "sessions" / "2026" / "09" / "04" /
                     "rollout-2026-09-04T10-00-00-thread-y.jsonl", "root-3", "thread-y", turns=3)
    entries = sync._catalogue("codex", set(), None, "")
    assert [(e["session_id"], len(e["turns"])) for e in entries] == [("root-3", 3)]


def test_session_lookup_returns_the_root_conversation(home, capsys) -> None:
    subagent_rollout(home.codex / "sessions" / "2026" / "09" / "05" /
                     "rollout-2026-09-05T10-00-00-thread-z.jsonl", "root-4", "thread-z", turns=5)
    archived_rollout(home.codex, "root-4", turns=2)
    code = sync.main(["--host", "codex", "--session", "root-4"])
    assert code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    items = [item for batch in lines for item in batch["items"]]
    texts = json.dumps(items)
    assert "subagent task" not in texts and "codex question 1" in texts


# ---------------------------------------------------------------------------
# The offer hook: a slow preview must not swallow the offer
# ---------------------------------------------------------------------------

def test_offer_summary_is_none_when_preview_is_slow(home, monkeypatch) -> None:
    import threading

    release = threading.Event()

    def slow_preview(*_args, **_kwargs):
        release.wait(30)  # stands in for reading a very large archive
        return {"sessions": 1}

    monkeypatch.setattr(sync, "_preview", slow_preview)
    monkeypatch.setattr(sync, "OFFER_PREVIEW_SECONDS", 0.2)
    start = time.perf_counter()
    try:
        assert sync._offer_summary("codex", str(home.data), "") is None
        assert time.perf_counter() - start < 2.0
    finally:
        release.set()


def test_offer_summary_returns_counts_when_fast(home, capsys) -> None:
    sessions_rollout(home.codex, "fast-1")
    summary = sync._offer_summary("codex", str(home.data), "")
    assert summary is not None and summary["sessions"] == 1
    assert summary["coverage"] == "Found 1 conversation. " + NOTE


def test_offer_hook_still_emits_when_preview_is_slow(home, monkeypatch, capsys) -> None:
    import threading

    sessions_rollout(home.codex, "slow-1")
    release = threading.Event()
    real_preview = sync._preview

    def slow_preview(host, data_dir, explicit, force=False):
        release.wait(30)
        return real_preview(host, data_dir, explicit, force)

    monkeypatch.setattr(sync, "_preview", slow_preview)
    monkeypatch.setattr(sync, "OFFER_PREVIEW_SECONDS", 0.2)
    monkeypatch.setattr(sync, "_hook_payload", lambda: {"session_id": "now-1"})
    monkeypatch.setattr(sync, "_connected", lambda data_dir: True)
    monkeypatch.setattr(sync, "_signed_out_locally", lambda host: False)
    try:
        assert sync._cmd_offer_check("codex", str(home.data)) == 0
    finally:
        release.set()
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Import offer pending" in context
    assert "--preview" in context and "I found" not in context
