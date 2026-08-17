from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(PLUGIN_ROOT))

from substrate_wiki import SubstrateWikiProvider  # noqa: E402
from substrate_wiki.client import SubstrateAPIError, SubstrateClient  # noqa: E402

CAPABILITIES = {
    "provider": "substrate_wiki",
    "server_commit": "b" * 40,
    "capture_schema_versions": [2],
    "max_event_bytes": 262_144,
    "history_replay": {
        "protocol": "stream-v2",
        "min_plugin_version": "1.2.0",
        "content_free_completion": True,
        "incremental_windows": True,
        "status_version": 2,
    },
    "entity_memory": {
        "protocol": "entity-wiki-v1",
        "min_plugin_version": "1.3.0",
        "search_endpoint": "/api/v1/hermes/memory/search",
        "canonical_wiki_pages": True,
        "entity_page_type": "entity",
    },
    "entity_quality": {
        "protocol": "entity-quality-v2",
        "min_plugin_version": "1.4.0",
        "memory_card": True,
        "quality_version": 2,
        "canonical_redirects": True,
    },
}


def test_client_requires_semantic_capability_then_uses_entity_memory_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self: SubstrateClient, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((method, path, kwargs))
        if path.endswith("/capabilities"):
            return CAPABILITIES
        return {
            "results": [
                {
                    "path": "entities/project/substrate--abc12345.md",
                    "canonical_path": "entities/project/substrate--abc12345.md",
                    "page_type": "entity",
                    "entity_id": "entity-substrate",
                    "entity_type": "project",
                    "quality_version": 2,
                    "memory_card": "Substrate project profile",
                    "content": "full page content must not enter automatic recall",
                }
            ]
        }

    monkeypatch.setattr(SubstrateClient, "request", request)
    client = SubstrateClient("https://wiki.example.test", "key")

    first = client.memory_search("substrate", scope={"platform": "cli"})
    second = client.memory_search("project", scope={"platform": "cli"})

    assert first["results"][0]["path"].startswith("entities/")
    assert first["results"][0]["memory_card"] == "Substrate project profile"
    assert second["results"]
    assert [path for _, path, _ in calls] == [
        "/api/v1/hermes/capabilities",
        "/api/v1/hermes/memory/search",
        "/api/v1/hermes/memory/search",
    ]
    assert calls[1][0] == "POST"
    assert calls[1][2]["body"]["platform"] == "cli"
    assert calls[1][2]["body"]["q"] == "substrate"


def test_capability_shaping_preserves_only_the_content_free_server_commit() -> None:
    shaped = SubstrateClient._shape_response("/api/v1/hermes/capabilities", CAPABILITIES)

    assert shaped["server_commit"] == "b" * 40


def test_memory_response_shaping_drops_full_page_content() -> None:
    shaped = SubstrateClient._shape_response(
        "/api/v1/hermes/memory/search",
        {
            "results": [
                {
                    "path": "entities/project/substrate--abc12345.md",
                    "canonical_path": "entities/project/substrate--abc12345.md",
                    "page_type": "entity",
                    "entity_id": "entity-substrate",
                    "entity_type": "project",
                    "quality_version": 2,
                    "memory_card": "Substrate project profile",
                    "content": "full page content must not enter automatic recall",
                    "snippet": "full body snippet must not enter automatic recall",
                }
            ]
        },
    )

    result = shaped["results"][0]
    assert result["memory_card"] == "Substrate project profile"
    assert "content" not in result
    assert "snippet" not in result


def test_client_rejects_old_server_for_automatic_entity_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SubstrateClient,
        "request",
        lambda self, method, path, **kwargs: {
            key: value for key, value in CAPABILITIES.items() if key != "entity_memory"
        },
    )
    client = SubstrateClient("https://wiki.example.test", "key")
    with pytest.raises(SubstrateAPIError, match="server_upgrade_required"):
        client.memory_search("anything")


def test_client_rejects_server_without_entity_quality_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SubstrateClient,
        "request",
        lambda self, method, path, **kwargs: {
            key: value for key, value in CAPABILITIES.items() if key != "entity_quality"
        },
    )
    with pytest.raises(SubstrateAPIError, match="server_upgrade_required"):
        SubstrateClient("https://wiki.example.test", "key").memory_search("anything")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol", "entity-quality-v1"),
        ("min_plugin_version", "2.0.1"),
        ("memory_card", False),
        ("quality_version", 1),
        ("canonical_redirects", False),
    ],
)
def test_client_rejects_downgraded_entity_quality_contract(
    field: str,
    value: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities = dict(CAPABILITIES)
    capabilities["entity_quality"] = {
        **CAPABILITIES["entity_quality"],
        field: value,
    }
    monkeypatch.setattr(
        SubstrateClient,
        "request",
        lambda self, method, path, **kwargs: capabilities,
    )

    with pytest.raises(SubstrateAPIError, match="server_upgrade_required"):
        SubstrateClient("https://wiki.example.test", "key").memory_search("anything")


def test_client_accepts_exact_current_plugin_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    capabilities = dict(CAPABILITIES)
    capabilities["entity_quality"] = {
        **CAPABILITIES["entity_quality"],
        "min_plugin_version": "1.5.0",
    }
    monkeypatch.setattr(
        SubstrateClient,
        "request",
        lambda self, method, path, **kwargs: capabilities,
    )

    SubstrateClient("https://wiki.example.test", "key").require_entity_wiki_capability()


def test_client_rejects_entity_capability_from_wrong_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SubstrateClient,
        "request",
        lambda self, method, path, **kwargs: {**CAPABILITIES, "provider": "other"},
    )
    with pytest.raises(SubstrateAPIError, match="server_upgrade_required"):
        SubstrateClient("https://wiki.example.test", "key").memory_search("anything")


def test_semantic_gate_accepts_lower_minimum_and_retries_after_server_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def request(self: SubstrateClient, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        if path.endswith("/capabilities"):
            calls += 1
            if calls == 1:
                return {key: value for key, value in CAPABILITIES.items() if key != "entity_memory"}
            upgraded = dict(CAPABILITIES)
            upgraded["entity_memory"] = {
                **CAPABILITIES["entity_memory"],
                "min_plugin_version": "1.2.9",
            }
            return upgraded
        return {"results": []}

    monkeypatch.setattr(SubstrateClient, "request", request)
    client = SubstrateClient("https://wiki.example.test", "key")
    with pytest.raises(SubstrateAPIError, match="server_upgrade_required"):
        client.memory_search("before")
    assert client.memory_search("after") == {"results": []}
    assert calls == 2


@pytest.mark.parametrize("minimum", ["1.3", "1.3.0-beta", "01.3.0", "01.x.0", "1.3.0.0"])
def test_semantic_gate_rejects_non_strict_versions(
    minimum: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capabilities = dict(CAPABILITIES)
    capabilities["entity_memory"] = {
        **CAPABILITIES["entity_memory"],
        "min_plugin_version": minimum,
    }
    monkeypatch.setattr(
        SubstrateClient,
        "request",
        lambda self, method, path, **kwargs: capabilities,
    )
    with pytest.raises(SubstrateAPIError, match="server_upgrade_required"):
        SubstrateClient("https://wiki.example.test", "key").memory_search("anything")


def test_v141_manifest_and_prompt_describe_one_published_memory() -> None:
    manifest = (PLUGIN_ROOT / "substrate_wiki" / "plugin.yaml").read_text(encoding="utf-8")
    prompt = SubstrateWikiProvider().system_prompt_block()
    assert "version: 2.0.2" in manifest
    assert "single published memory" in prompt
    assert "canonical published entity" in prompt
