# Security policy

## Supported version

Security fixes target the latest released `substrate_wiki` version. Version 1.5.0 targets Hermes 0.18.2 and is verified by the standalone contract suites documented in `COMPATIBILITY.md`; full host-lifecycle certification remains pending.

## Report a vulnerability

Use GitHub's private **Security advisories → Report a vulnerability** flow for this repository. Do not open a public issue containing a credential, private history, server URL, spool contents, traceback with user content, or exploit details.

Include only content-free diagnostics where possible:

- plugin and Hermes versions;
- operating system and Python version;
- affected lifecycle/tool operation;
- failure category;
- minimal synthetic reproduction;
- expected and observed security boundary.

Never send real `HERMES_API_KEY` values or production memory.

## Security boundary

The plugin:

- accepts credentials only from `HERMES_API_URL` and `HERMES_API_KEY` process environment variables;
- rejects unsafe redirects and unexpected response shapes;
- redacts before queueing, persistence, or network transfer;
- uses bounded request, response, spool, retry, and import resources;
- stores state below the active Hermes profile;
- requires immutable checksums and per-file provenance for installation.

Redaction cannot identify every sensitive statement. Operators remain responsible for choosing a trusted Substrate server and protecting `$HERMES_HOME`.

See `docs/threat-model.md` for the complete model.
