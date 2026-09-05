# Security policy

## Supported versions

The current supported install target is the `substrate` plugin (0.4.0) in
[`plugins/substrate`](plugins/substrate), certified for Hermes 0.21.0 exactly
and installed from the `v0.5.0` release tag. Do not install on an unverified
host version. It self-onboards and never requires a pasted API key.

## Report a vulnerability

Use GitHub's private **Security advisories → Report a vulnerability** flow for this
repository. Do not open a public issue for a suspected secret leak, auth bypass, or
remote-execution vector.

## Credential handling

- Tenant-scoped keys live only in the active profile's `.env` (`SUBSTRATE_API_KEY`) and
  the issuing server. They never enter source, artifacts, logs, errors, chat, or process
  arguments.
- Device codes live only in an owner-private profile file and are deleted on terminal
  states. Onboarding state files never contain access tokens.
- The plugin verifies TLS with the host system trust store. Verification is never
  disabled, no extra root is bundled, and leaf certificates are never pinned.

## Response validation

Every server response is schema-validated before use. Unknown or oversized payloads fail
closed. Tool results are bounded and redacted before they reach the model.

## Capture disclosure

- Completed turns sent to the server include bounded redacted tool traffic
  alongside user/assistant text: tool call arguments (at most 4096 bytes
  canonical JSON, or a truncated digest form) and tool result excerpts (at
  most 8192 bytes plus a SHA-256 digest of the full redacted result).
- System messages are never captured or uploaded.
- Undelivered capture waits only in the profile-local durable spool
  (`<profile>/substrate/spool`, directories 0700, files 0600) until delivery
  or bounded eviction; retrieved memory is never cached locally.
