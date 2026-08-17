# Hermes Substrate Wiki

`substrate_wiki` is the official Hermes integration for Substrate: **the memory that decides what your agents are allowed to do**. Version `2.0.0` connects one Hermes profile to hosted Substrate at `https://app.trysubstrate.co` through a bounded, cited memory interface and durable, redacted history capture.

The broader product uses memory to ground deterministic agent permissions. **Version 2.0.0 is the memory-provider integration only:** it does not authorize actions, include the local runtime or policy compiler, or support a no-server mode. Those gaps are explicit rather than silently presented as hosted-only features.

**This repository is the canonical editable source for the plugin.** `Substrate-v2` owns the server and HTTP contract; immutable plugin releases and their checksums are published from this repository.

## Status

| Surface | Supported |
|---|---|
| Plugin | `substrate_wiki` 2.0.0 |
| Hermes host | Targets 0.20.x; standalone contract suite verified |
| Server contract | `stream-v2`, `entity-wiki-v1`, `entity-quality-v2` |
| Runtime dependencies | Python standard library only |
| License | MIT |

Hermes 0.20.x provider discovery and setup hooks are targeted. The hosted origin is fixed; there is no local/self-hosted discovery path.

## Install with an agent

Give your local coding agent the URL of this README and the following instruction:

```text
Install the official Substrate plugin for my existing Hermes installation by following the
"Install with an agent" section of this README exactly.

Security and consent rules:
- Install only immutable release v2.0.3 from Substrate-memory/hermes-substrate-wiki.
- Before executing downloaded code, independently verify the installer and plugin archive
  against the exact SHA-256 values in the README. Never substitute a branch archive, CI
  artifact, newer release, or checksum obtained only from the same download response.
- Require `hermes --version` to report 0.20.x. If Hermes is absent or a different version,
  stop and tell me; do not install or upgrade Hermes yourself.
- Use the active HERMES_HOME/profile. Do not configure HERMES_API_URL or HERMES_API_KEY.
- Do not print, store, or request access tokens. The plugin's credential-custody flow owns them.
- Run the installer with `--yes --json`; add `--headless` if a browser cannot be opened.
- Immediately show me `verification_uri_complete` as the exact clickable authorization URL,
  plus the one-time code. Never show `verification_uri` alone. Then wait while I approve access
  in my browser; never approve access on my behalf.
- Historical upload is the only consent choice. Ask me whether to approve or decline it; do not
  infer consent. Declining history must leave future capture enabled.
- After onboarding, run the content-free onboarding-status command. Report only version,
  installation path, provider activation, connection phase, and import counts/status. Never
  expose conversation content or credentials.
- If installation or onboarding is interrupted, rerun the same verified installer or the
  documented repair command; do not create replacement credentials or import jobs unnecessarily.

Select the Bash or PowerShell procedure below for this machine. Clean up only the downloaded
installer/archive/checksum files when finished; do not remove plugin state, credentials, spool,
or checkpoints.
```

The instruction deliberately leaves browser approval and history consent with the human. The agent may install and verify the plugin, but it must not make either decision.

### Bash: Linux, macOS, or WSL

```bash
set -euo pipefail

hermes_version="$(hermes --version)"
case "$hermes_version" in
  *0.20.*) ;;
  *) printf 'Hermes 0.20.x is required; found: %s\n' "$hermes_version" >&2; exit 1 ;;
esac

install_dir="$(mktemp -d)"
chmod 700 "$install_dir"
cd "$install_dir"

base='https://github.com/Substrate-memory/hermes-substrate-wiki/releases/download/v2.0.3'
curl --fail --location --proto '=https' --tlsv1.2 --remote-name "$base/install_hermes_plugin.py"
curl --fail --location --proto '=https' --tlsv1.2 --remote-name "$base/substrate_wiki.zip"
curl --fail --location --proto '=https' --tlsv1.2 --remote-name "$base/SHA256SUMS"

python3 - <<'PY'
import hashlib
from pathlib import Path

expected = {
    "install_hermes_plugin.py": "72247d3537140098365350020cce29658c0743fee1aa738d7143db82316acce4",
    "substrate_wiki.zip": "dfaa786f68dd819e1313191bb26253caf6bc52fe4b0ab4f6f8c2e2ebcb62e1a3",
}
for name, digest in expected.items():
    actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
    if actual != digest:
        raise SystemExit(f"{name}: checksum mismatch")
published = Path("SHA256SUMS").read_text(encoding="utf-8")
for name, digest in expected.items():
    if f"{digest}  {name}" not in published.splitlines():
        raise SystemExit(f"{name}: published SHA256SUMS mismatch")
print("Release checksums verified")
PY

python3 install_hermes_plugin.py \
  --archive substrate_wiki.zip \
  --sha256 dfaa786f68dd819e1313191bb26253caf6bc52fe4b0ab4f6f8c2e2ebcb62e1a3 \
  --yes --json
```

Add `--headless` to the final command on a machine that cannot open a browser.

### PowerShell: Windows

```powershell
$ErrorActionPreference = 'Stop'

$hermesVersion = (& hermes --version | Out-String).Trim()
if ($hermesVersion -notmatch '0\.20\.') {
    throw "Hermes 0.20.x is required; found: $hermesVersion"
}

$installDir = Join-Path ([IO.Path]::GetTempPath()) ("substrate-wiki-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $installDir | Out-Null
Set-Location $installDir

$base = 'https://github.com/Substrate-memory/hermes-substrate-wiki/releases/download/v2.0.3'
Invoke-WebRequest "$base/install_hermes_plugin.py" -OutFile 'install_hermes_plugin.py'
Invoke-WebRequest "$base/substrate_wiki.zip" -OutFile 'substrate_wiki.zip'
Invoke-WebRequest "$base/SHA256SUMS" -OutFile 'SHA256SUMS'

$installerSha = (Get-FileHash -Algorithm SHA256 'install_hermes_plugin.py').Hash.ToLowerInvariant()
$archiveSha = (Get-FileHash -Algorithm SHA256 'substrate_wiki.zip').Hash.ToLowerInvariant()
if ($installerSha -ne '72247d3537140098365350020cce29658c0743fee1aa738d7143db82316acce4') {
    throw 'Installer checksum mismatch'
}
if ($archiveSha -ne 'dfaa786f68dd819e1313191bb26253caf6bc52fe4b0ab4f6f8c2e2ebcb62e1a3') {
    throw 'Plugin archive checksum mismatch'
}
$published = Get-Content 'SHA256SUMS' -Raw
if (($published -notmatch [regex]::Escape($installerSha)) -or
    ($published -notmatch [regex]::Escape($archiveSha))) {
    throw 'Published SHA256SUMS does not match the independently pinned checksums'
}

py -3 install_hermes_plugin.py `
  --archive substrate_wiki.zip `
  --sha256 dfaa786f68dd819e1313191bb26253caf6bc52fe4b0ab4f6f8c2e2ebcb62e1a3 `
  --yes --json
```

Use `python` instead of `py -3` if that is the installed Python 3 launcher. Add `--headless` to the final command on a machine that cannot open a browser.

### Finish onboarding

The installer validates archive provenance, target-Hermes metadata, and packaged source digests; installs beneath `$HERMES_HOME/plugins/substrate_wiki`; activates `memory.provider: substrate_wiki`; and starts hosted device onboarding.

The installing agent must present `verification_uri_complete` exactly as the clickable authentication URL. Never present `verification_uri` alone: that bare URL requires manual code entry. The complete URL opens the passwordless email sign-in with the one-time Hermes code already attached.

After browser approval, answer the history prompt yourself:

- **Approve**: upload eligible direct conversations and explicit saved memories using durable checkpoints.
- **Decline**: do not upload past history; automatic future capture remains enabled.

If a non-interactive agent cannot present the consent prompt, have it ask you first and then run exactly one of:

```bash
hermes substrate_wiki onboard --mode device --wait --history approve --json
hermes substrate_wiki onboard --mode device --wait --history decline --json
```

Then verify the content-free state:

```bash
hermes substrate_wiki onboarding-status --json
```

Tenant credentials are stored in native credential custody, with an owner-private profile fallback; they never belong in ordinary configuration, logs, arguments, or diagnostics. See [configuration and operation](docs/operation.md) and the immutable [v2.0.3 release](https://github.com/Substrate-memory/hermes-substrate-wiki/releases/tag/v2.0.3).

## What it does

- Implements Hermes provider lifecycle hooks and tools.
- Prefetches bounded cited `memory_card` results for automatic recall.
- Captures completed turns, memory writes, and session boundaries.
- Redacts credential-shaped values before durable local spooling or network transfer.
- Replays Hermes history with deterministic event IDs and durable checkpoints.
- Keeps spool/checkpoint state beneath the active `$HERMES_HOME/substrate_wiki` profile.

It does **not** expose arbitrary filesystem writes, include Substrate server code, bundle credentials, or make private history public.

## Privacy boundary

Visible prompts, assistant output, tool calls, and tool results may be sent to the configured Substrate server after redaction. Redaction is defense in depth, not proof arbitrary sensitive prose is absent. Review the server's access and retention policy before enabling capture.

Local failed deliveries remain in a bounded owner-private spool until delivered or explicitly removed. Status, progress, and receipts are content-free.

Read [SECURITY.md](SECURITY.md), [the threat model](docs/threat-model.md), [the source-ownership boundary](docs/source-of-truth.md), and the permanent [open/held commercial boundary](BOUNDARY.md) before deployment.

## Open and paid boundary

The open side is permissively licensed and includes the Hermes plugin/client, memory extraction and entity model, credential containment, privacy deletion, and policy compiler. This Hermes integration itself is hosted-only. [BOUNDARY.md](BOUNDARY.md) distinguishes the permanent commitment from the current `v2.0.3` implementation.

The paid hosted tier covers hosted brokerage, multi-user operation, cross-organizational graph services, audit/attestation, and insurance-backed decisions. Its meter is per authorized action, never seats. This repository does not contain or license those held services.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
python -m compileall -q src scripts
python scripts/verify_public_plugin_candidate.py --root . --layout destination
```

Builds are deterministic and require an immutable source commit for releases:

```bash
HERMES_PLUGIN_SOURCE_COMMIT="$(git rev-parse HEAD)" python scripts/build_plugin.py
python scripts/build_plugin.py --check
```

Version `1.4.1` remains an imported immutable release with its original Substrate-v2 provenance. Repository-native releases begin at `2.0.0` and use standalone provenance.

## Repository map

- `src/substrate_wiki/` — canonical plugin source.
- `scripts/` — deterministic builder, verified installer, publication scanner, import benchmark.
- `tests/` — provider, privacy, replay, packaging, and resource-boundary tests.
- `legacy-assets/` — immutable releases imported from Substrate-v2.
- `docs/` — architecture, compatibility, ownership, release, and threat-model contracts.
- `BOUNDARY.md` — permanent open-source/commercial boundary and current implementation gaps.

## License

MIT © 2026 Sightline Technologies Inc. See [LICENSE](LICENSE).
