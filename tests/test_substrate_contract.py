"""Contract fixture round trip, digest literal, deterministic ids, ACK table."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid

import pytest

from substrate import contract as c

FIXTURES = c.load_fixtures()


def _case(section: str, name: str) -> dict:
    return next(case for case in FIXTURES[section] if case["name"] == name)


def test_fixture_digest_literal_matches_file() -> None:
    assert len(c.FIXTURE_SHA256) == 64 and int(c.FIXTURE_SHA256, 16) >= 0
    assert c.fixture_sha256() == c.FIXTURE_SHA256


def test_fixture_constants_agree_with_module() -> None:
    assert FIXTURES["contract_version"] == c.CONTRACT_VERSION == 1
    assert FIXTURES["schema_version"] == c.SCHEMA_VERSION == 3
    assert FIXTURES["namespace"] == str(c.NAMESPACE)
    assert FIXTURES["actions"] == sorted(c.ACTIONS)
    assert FIXTURES["kinds"] == sorted(c.KINDS)
    assert FIXTURES["plugin_postable_kinds"] == sorted(c.PLUGIN_POSTABLE_KINDS)
    assert FIXTURES["action_classes"] == sorted(c.ACTION_CLASSES)
    assert FIXTURES["error_categories"] == sorted(c.ERROR_CATEGORIES)
    assert {"page_propose", "upload"} <= c.KINDS
    assert not ({"page_propose", "upload"} & c.PLUGIN_POSTABLE_KINDS)


@pytest.mark.parametrize("case", FIXTURES["valid"], ids=lambda case: case["name"])
def test_valid_envelopes_pass(case: dict) -> None:
    c.validate_envelope(case["envelope"], idempotency_key=case["idempotency_key"])
    # Round trip through canonical bytes must not change validity.
    again = json.loads(c.canonical_json(case["envelope"]))
    c.validate_envelope(again, idempotency_key=case["idempotency_key"])


def test_required_valid_cases_present() -> None:
    names = {case["name"] for case in FIXTURES["valid"]}
    assert {"turn_with_tool", "session_end", "memory_write", "memory_forget", "consent",
            "replay_deterministic"} <= names


def test_turn_with_tool_shape() -> None:
    envelope = _case("valid", "turn_with_tool")["envelope"]
    messages = envelope["payload"]["messages"]
    tool = next(m for m in messages if m["role"] == "tool")
    assert tool["result_digest"] == hashlib.sha256(tool["content"].encode()).hexdigest()
    assistant = next(m for m in messages if "tool_calls" in m)
    assert assistant["tool_calls"][0]["id"] == tool["tool_call_id"]
    assert len(c.canonical_bytes(assistant["tool_calls"][0]["args"])) <= c.LIMITS["max_tool_call_bytes"]


@pytest.mark.parametrize("case", FIXTURES["invalid"], ids=lambda case: case["name"])
def test_invalid_envelopes_raise_listed_category(case: dict) -> None:
    envelope = case["envelope"]
    key = case.get("idempotency_key", envelope.get("event_id"))
    with pytest.raises(c.ContractError) as info:
        c.validate_envelope(envelope, idempotency_key=key if isinstance(key, str) else None)
    assert info.value.category == case["error"], info.value.detail
    assert info.value.category in c.ERROR_CATEGORIES


def test_validation_order_and_size() -> None:
    with pytest.raises(c.ContractError, match="invalid_request"):
        c.validate_envelope([])
    with pytest.raises(c.ContractError) as info:
        c.validate_envelope({"schema_version": 2, "contract_version": 0})
    assert info.value.category == "unsupported_schema"
    with pytest.raises(c.ContractError) as info:
        c.validate_envelope({"schema_version": 3, "contract_version": 0})
    assert info.value.category == "unsupported_contract"
    with pytest.raises(c.ContractError) as info:
        c.validate_envelope({"schema_version": 3, "contract_version": True})
    assert info.value.category == "unsupported_contract"
    with pytest.raises(c.ContractError) as info:
        c.validate_envelope({"schema_version": 3, "contract_version": 1})
    assert info.value.category == "invalid_request"
    envelope = copy.deepcopy(_case("valid", "session_end")["envelope"])
    envelope["payload"]["platform"] = "x" * c.LIMITS["max_event_bytes"]
    with pytest.raises(c.ContractError) as info:
        c.validate_envelope(envelope)
    assert info.value.category == "payload_too_large"


def test_server_written_kinds_need_explicit_allow() -> None:
    envelope = _case("invalid", "kind_not_postable")["envelope"]
    with pytest.raises(c.ContractError):
        c.validate_envelope(envelope)
    c.validate_envelope(envelope, allowed_kinds=c.KINDS)


def test_size_limits_enforced() -> None:
    base = copy.deepcopy(_case("valid", "turn_with_tool")["envelope"])
    call = base["payload"]["messages"][1]["tool_calls"][0]
    call["args"] = {"command": "x" * 4000}  # canonical {"command":"xxx"} = 4014 bytes
    c.validate_envelope(base)
    call["args"] = {"command": "x" * 4090}
    with pytest.raises(c.ContractError):
        c.validate_envelope(base)
    truncated = {"id": call["id"], "tool_name": "terminal", "args_truncated": True,
                 "args_sha256": "a" * 64, "args_preview": "x" * 1024}
    base["payload"]["messages"][1]["tool_calls"][0] = truncated
    c.validate_envelope(base)
    truncated["args_preview"] = "x" * 1025
    with pytest.raises(c.ContractError):
        c.validate_envelope(base)
    truncated["args_preview"] = "x"
    tool = base["payload"]["messages"][2]
    tool["content"] = "é" * 4096  # 8192 bytes, ok
    c.validate_envelope(base)
    tool["content"] = "é" * 4097
    with pytest.raises(c.ContractError):
        c.validate_envelope(base)


def test_deterministic_event_id_vector() -> None:
    case = _case("valid", "replay_deterministic")
    env = case["envelope"]
    got = c.deterministic_event_id(env["kind"], env["session_id"], env["offset"], env["payload"])
    assert got == case["expected_event_id"] == env["event_id"]
    assert uuid.UUID(got).version == 5
    # Re-keying the same object in a different insertion order changes nothing.
    reordered = json.loads(json.dumps(env["payload"]))
    assert c.deterministic_event_id(env["kind"], env["session_id"], env["offset"], reordered) == got
    # Any change to the payload changes the id.
    changed = copy.deepcopy(env["payload"])
    changed["messages"][0]["content"] += "!"
    assert c.deterministic_event_id(env["kind"], env["session_id"], env["offset"], changed) != got
    assert c.deterministic_event_id(env["kind"], env["session_id"], {"start": 1, "end": 3}, env["payload"]) != got


def test_canonical_json_is_stable() -> None:
    value = {"b": 1, "a": {"d": [3, {"z": 1, "y": 2}], "c": "é\n\x1f"}}
    assert c.canonical_json(value) == '{"a":{"c":"é\\n\\u001f","d":[3,{"y":2,"z":1}]},"b":1}'
    assert c.canonical_json({"é": 1, "z": 2, "\U0001F600": 3, "ﬁ": 4}) == '{"z":2,"é":1,"ﬁ":4,"😀":3}'


def test_ack_table() -> None:
    for case in FIXTURES["ack"]["valid"]:
        assert c.ack_ok(case["ack"], case["event_id"]), case["name"]
    for case in FIXTURES["ack"]["reject"]:
        assert not c.ack_ok(case["ack"], case["event_id"]), case["name"]
    assert not c.ack_ok(None, "x")
    assert not c.ack_ok({"stored": 1, "event_id": "x", "action": "stored"}, "x")


def test_fixture_requests_and_responses_validate() -> None:
    c.validate_turn_context_request(FIXTURES["requests"]["turn_context"])
    c.validate_action_cues_request(FIXTURES["requests"]["action_cues"])
    caps = c.validate_capabilities(FIXTURES["responses"]["capabilities"])
    assert caps["tenant"]["brief_version"] == 7
    ctx = c.validate_turn_context(FIXTURES["responses"]["turn_context"])
    assert ctx["handles"] and ctx["empty_reason"] == ""
    cues = c.validate_action_cues(FIXTURES["responses"]["action_cues"])
    assert len(cues["notes"]) == 1
    rules = c.validate_rules(FIXTURES["responses"]["rules"])
    assert rules["rules"][0]["enforce"] is True


def test_response_validators_reject_drift() -> None:
    tc = FIXTURES["responses"]["turn_context"]
    for patch in ({"block": "x" * 8193}, {"block": "\n".join(["- l"] * 41)}, {"empty_reason": "later"},
                  {"handles": ["x:1"]}, {"contract_version": 2}, {"latency_ms": "fast"}):
        with pytest.raises(c.ContractError) as info:
            c.validate_turn_context({**tc, **patch})
        assert info.value.category == "invalid_response", patch
    ac = FIXTURES["responses"]["action_cues"]
    with pytest.raises(c.ContractError):
        c.validate_action_cues({**ac, "notes": [ac["notes"][0]] * 4})
    with pytest.raises(c.ContractError):
        c.validate_action_cues({**ac, "notes": [{**ac["notes"][0], "text": "t" * 161}]})
    rules = FIXTURES["responses"]["rules"]
    with pytest.raises(c.ContractError):
        c.validate_rules({**rules, "rules": [{**rules["rules"][0], "enforce": False}]})
    caps = FIXTURES["responses"]["capabilities"]
    with pytest.raises(c.ContractError) as info:
        c.validate_capabilities({**caps, "contract_version": 2})
    assert info.value.category == "unsupported_contract"
    with pytest.raises(c.ContractError):
        c.validate_capabilities({**caps, "actions": ["stored"]})
    # Unknown top-level response fields are dropped by shaping, not rejected.
    shaped = c.shape_response("turn_context", {**tc, "debug": {"x": 1}, "turn": "4"})
    assert "debug" not in shaped and "turn" not in shaped


def test_request_validators_reject_drift() -> None:
    tc = FIXTURES["requests"]["turn_context"]
    with pytest.raises(c.ContractError) as info:
        c.validate_turn_context_request({**tc, "contract_version": 2})
    assert info.value.category == "unsupported_contract"
    for patch in ({"message": "m" * 16385}, {"injected_handles": ["x:1"]}, {"extra": 1},
                  {"recent_turns": [{"user": "a", "assistant": "b"}] * 3}, {"deadline_ms": 0}):
        with pytest.raises(c.ContractError) as info:
            c.validate_turn_context_request({**tc, **patch})
        assert info.value.category == "invalid_request", patch
    ac = FIXTURES["requests"]["action_cues"]
    for patch in ({"action_class": "run"}, {"artifact_keys": [{"kind": "path", "key": "/x"}] * 33},
                  {"artifact_keys": [{"kind": "file", "key": "/x"}]}):
        with pytest.raises(c.ContractError):
            c.validate_action_cues_request({**ac, **patch})


def test_response_fields_cover_every_route() -> None:
    assert set(c.RESPONSE_FIELDS) == {
        "capabilities", "events", "turn_context", "action_cues", "rules", "search", "expand",
        "evidence", "propose", "pinned", "upload", "job_status", "import_status",
    }
    for fields in c.RESPONSE_FIELDS.values():
        for types in fields.values():
            assert isinstance(types, tuple) and all(isinstance(t, type) for t in types)


def test_handle_regex() -> None:
    assert c.HANDLE_RE.match("m:44a1b02e") and c.HANDLE_RE.match("p:" + "f" * 64)
    for bad in ("m:44A1B02E", "m:1234567", "x:44a1b02e", "m:" + "f" * 65, "44a1b02e", ""):
        assert not c.HANDLE_RE.match(bad), bad
