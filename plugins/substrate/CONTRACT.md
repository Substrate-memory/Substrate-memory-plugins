# Substrate ↔ Hermes plugin wire contract

`contract_version` **1**, `schema_version` **3**. This file is byte-identical in the plugin repository (`hermes-substrate/CONTRACT.md`) and the server repository (`Substrate-v5 experiment/CONTRACT.md`). It is implemented twice, by `src/substrate/contract.py` (plugin, Python ≥ 3.11, stdlib only) and `contract.mjs` (server, Node 22, zero dependencies), and both implementations are checked against the same fixture file `contract/envelope-fixtures.json`, also byte-identical in both repositories.

Fixture SHA-256: 627615398b726d04f32b5bab58b480b00ba85ca80c65d66864d7e8ea1a30ab85

Both test suites assert that literal against the file on disk. A change to the fixture is a change to this file and to both `FIXTURE_SHA256` literals in the same commit on both sides.

Conventions used below: all string sizes are UTF-8 byte counts; "≤ N" means at most N bytes; integers are JSON numbers without a fraction inside ±2⁵³−1; timestamps are RFC 3339 UTC `YYYY-MM-DDTHH:MM:SS[.ffffff]Z`; `hex` is lowercase; a UUID is the lowercase 8-4-4-4-12 form with version nibble 4 or 5. Objects are closed: an unknown field is a validation error unless a section says otherwise.

## 1. Versions and negotiation

- `contract_version` is an integer, currently `1`. Every request body the plugin builds and every response body the server returns carries it. The plugin refuses to run unless `GET /api/v1/capabilities` reports `contract_version: 1`. The server answers `400 {"error":"unsupported_contract"}` to any other value.
- `schema_version` is the envelope schema, currently `3`. The server answers `400 {"error":"unsupported_schema"}` to any other value.
- Neither version number is negotiated; both sides ship exactly one.

## 2. Authentication and scopes

- `Authorization: Bearer sk_sub_<32 url-safe characters>`. The server stores only `sha256(key)`.
- Scopes: `capture` (`/ledger/events`, `/ledger/upload`, `/import-status`), `retrieve` (`/memory/*`, `/pages/*`, `/jobs/*`), `admin`. The device grant issues `capture retrieve`.
- Missing or unknown key → `401 {"error":"unauthorized"}`. Known key without the scope → `403 {"error":"forbidden"}`. The plugin treats either as "reconnect": the token is deleted, onboarding restarts in the background, and spooled events stay spooled.

## 3. Error shape

Every non-2xx body is exactly `{"error": <category>}`; no other fields, no free text. Categories:

`unauthorized`, `forbidden`, `invalid_request`, `unsupported_contract`, `unsupported_schema`, `payload_too_large`, `not_found`, `conflict`, `rate_limited`, `internal`.

HTTP status by category: 401, 403, 400, 400, 400, 413, 404, 409, 429 (with `Retry-After`), 500. The plugin additionally uses the local categories `invalid_response`, `transport_error` and `timeout`, which never appear on the wire.

## 4. Capabilities

`GET /api/v1/capabilities` (any scope) →

```json
{
  "contract_version": 1,
  "provider": "substrate",
  "server_commit": "<≤64>",
  "limits": {
    "max_event_bytes": 262144, "max_upload_bytes": 262144,
    "max_tool_call_bytes": 4096, "max_tool_result_bytes": 8192,
    "turn_context_deadline_ms": 500, "action_cues_deadline_ms": 100,
    "rules_refresh_seconds": 300
  },
  "actions": ["stored", "duplicate", "sealed", "queued"],
  "kinds": ["capture_turn", "capture_session", "memory_write", "memory_forget", "consent", "page_propose", "upload"],
  "tenant": {"tenant_id": "<≤128>", "brief_version": 0}
}
```

The plugin validates: `contract_version == 1` (else `unsupported_contract`), `provider == "substrate"`, every limit present as a non-negative integer, `actions` ⊇ the four known actions, `kinds` ⊇ the five plugin-postable kinds, `tenant.brief_version` integer ≥ 0.

## 5. Ledger event envelope

`POST /api/v1/ledger/events`, scope `capture`, header `Idempotency-Key` equal to the body's `event_id`, body ≤ 256 KiB.

```json
{
  "schema_version": 3,
  "contract_version": 1,
  "event_id": "<uuid>",
  "kind": "<kind>",
  "session_id": "<1..512>",
  "offset": {"start": 0, "end": 0},
  "capture_origin": "live" | "history_replay" | "catchup",
  "batch_id": "" | "<8..64 hex>",
  "speaker": {"id": "<1..256>", "role": "owner" | "participant" | "agent", "display": "<≤256>"},
  "created_at": "<timestamp>",
  "payload": { ... }
}
```

All eleven fields are required; no others are allowed. `offset.start ≥ 0`, `offset.end ≥ offset.start`. The payload contains integers only (no fractional numbers) so canonical bytes are identical across runtimes.

### 5.1 Kinds

Known kinds: `capture_turn`, `capture_session`, `memory_write`, `memory_forget`, `consent`, `page_propose`, `upload`. The plugin may post the first five (`PLUGIN_POSTABLE_KINDS`). `page_propose` is written by the server from `POST /pages/propose` and `upload` from `POST /ledger/upload`; either posted to `/ledger/events` is `invalid_request`. Every kind, including `consent` and `page_propose`, is stored as a version of its canonical payload bytes so replay has one uniform input.

### 5.2 `capture_turn`

```
payload: {turn_id: <1..128>, messages: [1..4096 message]}
message: {
  index: int ≥ 0, role: "user" | "assistant" | "tool", content: string,
  timestamp?: <timestamp>, speaker?: <speaker>, fragment?: <fragment>,
  tool_calls?: [≤64 tool_call]                       -- assistant only
  tool_call_id?: <1..128>, tool_name?: <1..128>,     -- tool only
  result_digest: <sha256 hex>, result_bytes: int ≥ 0, result_truncated?: bool   -- tool only, digest+bytes required
}
tool_call: {id: <1..128>, tool_name: <1..128>, args: object}                     -- canonical JSON of args ≤ 4096
        | {id, tool_name, args_truncated: true, args_sha256: <sha256 hex>, args_preview: <≤1024>}
fragment: {encoding: "utf8-content" | "canonical-json", index: int, count: int ≥ 1, sha256: <sha256 hex>}  -- index < count
```

Rules: a tool message's `content` is the redacted excerpt, ≤ 8192 bytes, and `result_digest` is the SHA-256 of the full redacted result (equal to the digest of `content` when not truncated). `args` are redacted before sizing; the truncated form carries no `args`. Message `index` values are non-decreasing and strictly increasing except between fragments of the same message. `offset.start == messages[0].index` and `offset.end == messages[-1].index + 1`.

### 5.3 `capture_session` (content-free)

```
payload: {boundary: "end" | "switch" | "reset" | "rewound" | "compress", session_complete: bool,
          next_session_id?: <1..512>, parent_session_id?: <1..512>, message_high_water: int ≥ 0,
          platform: <1..64>, chat_type: <1..32>, participants?: [≤64 {id: <1..256>, display: <≤256>}]}
```

`session_complete: true` seals the session's extraction window on the server (ACK action `sealed`).

### 5.4 `memory_write`

```
payload: {text: <1..4096>, about?: <≤256>, durability: "durable" | "time_bounded" | "transient",
          source: "memory_remember" | "hermes_memory_tool", action?: <≤64>, target?: <≤256>}
```

### 5.5 `memory_forget`

```
payload: {handle: <handle>, reason: <≤1024>}
```

### 5.6 `consent`

```
payload: {version: 1, scope: "hermes_history", decision: "approved" | "declined" | "revoked",
          recorded_at: <timestamp>, includes_other_profiles: bool}
```

### 5.7 `page_propose` (server-written)

```
payload: {page_id: <1..128>, title: <1..200>, prompt: <1..4096>, session_id: <1..512>}
```

### 5.8 `upload` (server-written manifest)

```
payload: {title: <1..512>, filename?: <≤256>, sha256: <sha256 hex>, byte_size: int ≤ 262144,
          source: "content" | "url", url?: <https URL ≤2048, required iff source = "url">}
```

### 5.9 Handles

`^[mp]:[0-9a-f]{8,64}$`. `m:` is a memory unit, `p:` is a page. `memory_remember` returns the provisional handle `m:` + `sha256(event_id)[:8]`.

### 5.10 Event ids

- Live capture: UUID v4.
- History replay and catch-up: `uuid5(NAMESPACE, canonical_json({kind, session_id, offset, payload}))` with `NAMESPACE = 6f3a2b1c-9d8e-4f70-a1b2-c3d4e5f60718`. The name is the canonical JSON string encoded as UTF-8. Retries and resumes therefore reproduce the same id, and the server answers `duplicate` instead of storing twice.

### 5.11 Canonical JSON

Keys sorted recursively by Unicode code point, separators `,` and `:` with no whitespace, non-ASCII characters emitted raw (not `\uXXXX`), control characters escaped as JSON requires, integers only. Python: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. JS: `JSON.stringify` of a recursively key-sorted copy (code-point order). Strings must be valid Unicode scalar sequences (no lone surrogates).

### 5.12 Server validation order

1. Authentication → `unauthorized`.
2. Scope → `forbidden`.
3. Body size ≤ 262144 bytes → `payload_too_large`.
4. JSON parse; body must be an object → `invalid_request`.
5. `schema_version == 3` → `unsupported_schema`.
6. `contract_version == 1` → `unsupported_contract`.
7. `event_id` is a UUID and equals `Idempotency-Key` → `invalid_request`.
8. `kind` known and postable → `invalid_request`.
9. Every envelope field and the per-kind payload → `invalid_request`.

The server never ACKs an envelope that fails any step. Both contract modules run steps 3–9 in this order on a parsed body so the plugin can predict the server's answer offline.

## 6. ACK

`200 {"stored": true, "event_id": "<uuid>", "action": "stored" | "duplicate" | "sealed" | "queued", "job_id"?: "<≤128>"}`

The plugin retires the spool item **iff** HTTP status is 200 **and** `stored` is boolean `true` **and** `event_id` equals the posted id **and** `action` is one of the four known actions. Anything else (a 2xx with another shape, a missing field, a string `"true"`, an unknown action, a v2-style `{ok: true}`) is a transient failure: the claim is released and the sender backs off. 401/403 trigger reconnect; the item stays spooled. `ack_ok(ack, event_id)` / `ackOk(ack, eventId)` implement exactly this rule.

## 7. Device grant and sign-in

- `POST /oauth/device_authorization`, form `client_id=substrate-hermes&scope=capture retrieve` → `200 {device_code: <64 hex>, user_code: "XXXX-XXXX" (alphabet BCDFGHJKLMNPQRSTVWXZ23456789), verification_uri, verification_uri_complete, expires_in: 900, interval: 5}`.
- `POST /oauth/token`, form `grant_type=urn:ietf:params:oauth:grant-type:device_code&device_code=…&client_id=substrate-hermes` → `400 {"error": "authorization_pending" | "slow_down" | "expired_token" | "access_denied"}` or, once, `200 {access_token: "sk_sub_…", token_type: "Bearer", scope: "capture retrieve", tenant_id, account_id}`. Polling faster than `interval` answers `slow_down` and adds 5 s to the interval. The plugin requires `token_type == "Bearer"` and the exact scope set.
- Magic-link pages: `GET /connect?user_code=` (email form) → `POST /connect/email` (`email`, `user_code`, `csrf`) → `GET /connect/verify?token=` (single-use, 15 min; creates or attaches account and tenant; sets `sub_session` HttpOnly SameSite=Lax cookie, 24 h; redirects to `/connect/approve?user_code=`) → `POST /connect/approve` (`user_code`, `csrf`, `decision=approve|deny`) binds the grant to (tenant, account). Every `/connect/*` POST requires a valid `csrf`. `/oauth/*` and `/connect/*` are rate limited to 10 requests per minute per IP → `429` with `Retry-After`.
- Development only, when `SUBSTRATE_DEV_AUTH=1`: `POST /connect/dev/approve {"user_code", "email"}` approves a grant without a browser.

## 8. Recall routes (scope `retrieve`)

### 8.1 `POST /api/v1/memory/turn-context`

Request (all fields required):

```
{contract_version: 1, session_id: <1..512>, turn_id: <1..128>, turn: int ≥ 0, platform: <≤64>, chat_type: <≤32>,
 sender_id: <≤256>, agent_identity: <≤256>, agent_context: <≤32>, parent_session_id: <≤512>,
 message: <≤16384, redacted>, recent_turns: [≤2 {user: <≤4096>, assistant: <≤4096>}],
 injected_handles: [≤64 handle], cited_handles: [≤64 handle], deadline_ms: 1..5000}
```

Response:

```
{contract_version: 1, session_id, turn, block: <≤8192, ≤40 lines>, handles: [≤64 handle], tail_handles: [≤64 handle],
 brief_version: int ≥ 0, latency_ms: number, empty_reason: "" | "no_candidates" | "gated" | "not_implemented"}
```

The plugin's deadline is 500 ms wall clock. On deadline, transport error or an invalid response it injects the previous turn's block iff that block belongs to the same session and to `turn − 1`; otherwise nothing. A `brief_version` higher than the cached one triggers a background `GET /pages/pinned`. The server logs a content-free `retrieval_calls` row per request. Until the retrieval layer exists the server returns `block: ""` and `empty_reason: "not_implemented"`.

### 8.2 `POST /api/v1/memory/action-cues`

Request:

```
{contract_version: 1, session_id, turn_id, tool_call_id: <1..128>, tool_name: <1..128>,
 action_class: "read" | "write" | "execute" | "network" | "deploy" | "delete" | "delegate" | "other",
 artifact_keys: [≤32 {kind: "path" | "url" | "host" | "repo" | "email" | "ticket", key: <1..512>}], deadline_ms: 1..5000}
```

Response: `{contract_version: 1, tool_call_id, notes: [≤3 {handle, text: <1..160, single line>, enforce: bool}], latency_ms}`. Plugin deadline 100 ms; any failure yields no notes. Stub: `notes: []`.

### 8.3 `GET /api/v1/memory/rules`

`{contract_version: 1, rules_version: int ≥ 0, rules: [≤200 {handle, text: <1..200, single line>, action_classes: [action_class], artifact_keys: [≤32 artifact_key], enforce: true}]}`. Only enforceable rules are served; the plugin caches them locally, refreshes every 300 s, and blocks tool calls from the cache without touching the network. Stub: `rules: []`, `rules_version: 0`.

### 8.4 Search, expand, evidence

- `POST /memory/search {query: <1..4096>, kinds?: [≤16 <1..32>], limit?: 1..20 (default 8)}` → `{contract_version, results: [{handle, text, score, kind, markers}]}` (stub `[]`).
- `POST /memory/expand {handle}` → for `p:` `{contract_version, handle, kind: "page", title, abstract, markdown: <≤65536>}`; for `m:` `404 not_found` until retrieval exists.
- `POST /memory/evidence {handle, raw?: bool (default false), limit?: 1..20 (default 5)}` → `{contract_version, excerpts: [...], raw?: string}` (stub `[]` / `404`).

## 9. Pages (scope `retrieve`)

- `POST /api/v1/pages/propose {title: <1..200>, prompt: <1..4096>, session_id: <1..512>}` → `202 {contract_version, handle: "p:<page_id>", page_id, status: "queued", job_id}`. The server writes a `page_propose` ledger event, a `wiki_pages` row (`page_kind='custom'`, `proposed_by_agent=1`, `created_by='agent'`) and a `render_page` job.
- `GET /api/v1/pages/pinned?scope_kind=&scope_id=` → `{contract_version, brief_version, brief: {handle, title, abstract, markdown} | null, pinned: [{handle, title, mode: "abstract" | "full", abstract, markdown?}]}`. Stub: `brief: null`, `pinned: []`.

## 10. Upload, jobs, import status

- `POST /api/v1/ledger/upload` (scope `capture`) `{title: <1..512>, content?: <1..262144>, url?: <https ≤2048>, filename?: <≤256>}` with exactly one of `content` / `url` → `202 {contract_version, job_id, version_id, action: "queued" | "duplicate"}`. `Idempotency-Key` replays return the stored response. Oversized `content` → `payload_too_large`.
- `GET /api/v1/jobs/{job_id}` (scope `retrieve`) → `{contract_version, job_id, kind, status: "queued" | "running" | "completed" | "failed", attempts, created_at, finished_at, error_class, result}`; another tenant's job is `404`.
- `GET /api/v1/import-status?batch_id=` (scope `capture`) → `{contract_version, batch_id, events_received, sessions_seen, sessions_completed, versions_created, extracted, extraction_failed, pending, complete, last_event_at}`. `complete` is true iff at least one event was received, every session seen was closed by a `capture_session` with `session_complete: true`, and `pending == 0`. This is the only authoritative completion signal for an import.

## 11. Response shaping on the plugin side

The plugin shapes every response through a per-route allowlist of top-level fields and their types (`RESPONSE_FIELDS` in `contract.py`); unknown fields are dropped, mistyped fields are dropped, and the route validators then require what they need. Nothing enters the prompt that has not passed `validate_turn_context`, `validate_action_cues` or `validate_rules`.

## 12. Fixture file

`contract/envelope-fixtures.json` sections:

- `contract_version`, `schema_version`, `namespace`, `actions`, `kinds`, `plugin_postable_kinds`, `action_classes`, `error_categories` — must equal the module constants.
- `valid[]` — `{name, idempotency_key, envelope}` plus `expected_event_id` on `replay_deterministic`. Includes `turn_with_tool` (a `role: tool` message with `result_digest` and an assistant `tool_calls` entry), `group_chat_turn`, `session_end`, `session_switch`, `memory_write`, `memory_forget`, `consent`, `replay_deterministic`.
- `invalid[]` — `{name, error, envelope, idempotency_key?}`; every case must fail with exactly `error` on both sides.
- `ack.valid[]` / `ack.reject[]` — `{name, event_id, ack}`.
- `requests.turn_context`, `requests.action_cues` — valid request bodies.
- `responses.capabilities`, `responses.turn_context`, `responses.action_cues`, `responses.rules` — valid response bodies.

Both test suites: every `valid` passes, every `invalid` fails with the listed category, the ACK table holds, `deterministic_event_id` reproduces `expected_event_id`, and `FIXTURE_SHA256 == sha256(file)`.
