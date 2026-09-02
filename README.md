# Substrate Memory Plugins

This public repository contains the official Substrate memory plugins. The current plugin,
`substrate_wiki` 2.0.5, connects an existing Hermes 0.20.x agent to hosted Substrate at
`https://app.trysubstrate.co`.

The Substrate application and server are maintained in a separate private repository. This
repository is the canonical editable source for public plugin/client code, verified installers,
tests, documentation, and immutable release artifacts.

## Install with an agent

Tell your Hermes agent exactly:

```text
Install the memory plug-in at https://github.com/Substrate-memory/Substrate-memory-plugins
```

If you are the installing agent, follow the verified procedure below. Keep the installation
command running while the user approves the browser connection. Browser approval completes the
local device-code poll and returns control to the same conversation. Immediately say that the
connection succeeded, then ask:

> Do you want to import your existing Hermes conversation history into Substrate?

Do not select, infer, or pre-authorize the answer. Do not begin history discovery or upload until
the user explicitly answers yes. A no leaves future conversation capture enabled. A blank,
interrupted, or missing answer leaves consent pending.

Release `v2.0.5` is not published yet. Do not run the download commands below until this
notice is removed and exact SHA-256 values replace the pending markers.

Security requirements:

- Install only immutable release v2.0.5 from `Substrate-memory/Substrate-memory-plugins`.
- Verify the installer and plugin archive against the exact SHA-256 values below before executing
  downloaded code.
- Require `hermes --version` to report 0.20.x. Do not install or upgrade Hermes automatically.
- Use the active `HERMES_HOME` profile. Never inspect or copy another profile.
- Do not configure `HERMES_API_URL` or `HERMES_API_KEY`.
- Never print, request, or place access tokens in command arguments or logs.
- Present `verification_uri_complete` exactly when manual browser opening is needed, then keep
  polling. Never approve the browser connection for the user.
- When the installer returns `action_required: history_consent`, resume this same conversation
  and ask the history question above.
- Report only content-free installation, connection, and import status.

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

base='https://github.com/Substrate-memory/Substrate-memory-plugins/releases/download/v2.0.5'
curl --fail --location --proto '=https' --tlsv1.2 --remote-name "$base/install_hermes_plugin.py"
curl --fail --location --proto '=https' --tlsv1.2 --remote-name "$base/substrate_wiki.zip"
curl --fail --location --proto '=https' --tlsv1.2 --remote-name "$base/SHA256SUMS"

python3 - <<'PY'
import hashlib
from pathlib import Path

expected = {
    "install_hermes_plugin.py": "INSTALLER_SHA256_PENDING",
    "substrate_wiki.zip": "PLUGIN_SHA256_PENDING",
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
  --sha256 PLUGIN_SHA256_PENDING \
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

$base = 'https://github.com/Substrate-memory/Substrate-memory-plugins/releases/download/v2.0.5'
Invoke-WebRequest "$base/install_hermes_plugin.py" -OutFile 'install_hermes_plugin.py'
Invoke-WebRequest "$base/substrate_wiki.zip" -OutFile 'substrate_wiki.zip'
Invoke-WebRequest "$base/SHA256SUMS" -OutFile 'SHA256SUMS'

$installerSha = (Get-FileHash -Algorithm SHA256 'install_hermes_plugin.py').Hash.ToLowerInvariant()
$archiveSha = (Get-FileHash -Algorithm SHA256 'substrate_wiki.zip').Hash.ToLowerInvariant()
if ($installerSha -ne 'INSTALLER_SHA256_PENDING') {
    throw 'Installer checksum mismatch'
}
if ($archiveSha -ne 'PLUGIN_SHA256_PENDING') {
    throw 'Plugin archive checksum mismatch'
}
$published = Get-Content 'SHA256SUMS' -Raw
if (($published -notmatch [regex]::Escape($installerSha)) -or
    ($published -notmatch [regex]::Escape($archiveSha))) {
    throw 'Published SHA256SUMS does not match the independently pinned checksums'
}

py -3 install_hermes_plugin.py `
  --archive substrate_wiki.zip `
  --sha256 PLUGIN_SHA256_PENDING `
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

Tenant credentials are stored in native credential custody, with an owner-private profile fallback; they never belong in ordinary configuration, logs, arguments, or diagnostics. See [configuration and operation](docs/operation.md) and the immutable [v2.0.5 release](https://github.com/Substrate-memory/Substrate-memory-plugins/releases/tag/v2.0.5).

## What it does

- Implements Hermes provider lifecycle hooks and tools.
- Prefetches bounded cited `memory_card` results for automatic recall.
- Captures only completed user/assistant dialogue turns.
- Redacts credential-shaped values before durable local spooling or network transfer.
- Replays Hermes history with deterministic event IDs and durable checkpoints.
- Keeps spool/checkpoint state beneath the active `$HERMES_HOME/substrate_wiki` profile.

It does **not** expose arbitrary filesystem writes, include Substrate server code, bundle credentials, or make private history public.

## Privacy boundary

Visible prompts and assistant output may be sent to the configured Substrate server after redaction. Tool calls, tool results, system messages, memory-write events, session metadata, and provider scope are not uploaded. Redaction is defense in depth, not proof arbitrary sensitive prose is absent.

Local failed deliveries remain in a bounded owner-private spool until delivered or explicitly removed. Status, progress, and receipts are content-free.

Read [SECURITY.md](SECURITY.md), [the threat model](docs/threat-model.md), [the source-ownership boundary](docs/source-of-truth.md), and the permanent [open/held commercial boundary](BOUNDARY.md) before deployment.

## Open and paid boundary

The open side is permissively licensed and includes the Hermes plugin/client, memory extraction and entity model, credential containment, privacy deletion, and policy compiler. This Hermes integration itself is hosted-only. [BOUNDARY.md](BOUNDARY.md) distinguishes the permanent commitment from the current `v2.0.5` implementation.

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
