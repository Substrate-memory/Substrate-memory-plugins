# Security policy

## Supported versions

The standalone `v2.0.0` and `v2.0.1` releases and pending `v2.0.2` patch target Hermes 0.20.x and the hosted service at `https://app.trysubstrate.co`.
Candidate CI artifacts are not supported releases.

## Report a vulnerability

Use GitHub's private **Security advisories → Report a vulnerability** flow. Do not open a
public issue containing a credential, private history, spool contents, traceback with user
content, or exploit details. Include only content-free diagnostics: plugin/Hermes versions,
operating system, lifecycle operation, failure category, and a synthetic reproduction.

## Security boundary

The plugin:

- accepts hosted tenant credentials only through onboarding credential custody;
- fixes the network origin to `https://app.trysubstrate.co` and rejects unsafe overrides;
- stores credentials in a native vault when available, with a fail-closed owner-private fallback;
- never puts credentials in normal config, logs, errors, receipts, or command arguments;
- redacts before queueing, persistence, or network transfer;
- bounds requests, responses, spool size, retries, and import resources;
- keeps profile state beneath the active `$HERMES_HOME`;
- verifies immutable checksums, provenance, and packaged source hashes during installation.

Pattern redaction cannot identify every sensitive statement. Historical upload requires
explicit consent; declining it leaves future capture enabled. See `docs/threat-model.md`.
