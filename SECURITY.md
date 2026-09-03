# Security policy

## Supported versions

The current supported install target is the `substrate` plugin (0.3.x) in
[`plugins/substrate`](plugins/substrate), certified for Hermes 0.21.x and installed from
the `v0.3.0` release tag. It self-onboards and never requires a pasted API key.

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
- The plugin verifies TLS with the system trust store plus pinned public ISRG Root X1
  and X2 anchors. Verification is never disabled and leaf certificates are never pinned.

## Response validation

Every server response is schema-validated before use. Unknown or oversized payloads fail
closed. Tool results are bounded and redacted before they reach the model.
