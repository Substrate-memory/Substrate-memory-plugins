"""Verify the exact future public-plugin candidate is free of real secrets and endpoints.

The verifier is intentionally content-free: findings contain only a relative path and
finding class, never the matched value.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

SYNTHETIC_SENTINEL_PREFIX = "SUBSTRATE_SYNTHETIC_SECRET_DO_NOT_USE_"
SYNTHETIC_SENTINEL_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_]){SYNTHETIC_SENTINEL_PREFIX}[0-9]{{24}}(?![A-Za-z0-9_])"
)
SYNTHETIC_SENTINEL_CANDIDATE_PATTERN = re.compile(rf"{SYNTHETIC_SENTINEL_PREFIX}[A-Za-z0-9_]+")
SYNTHETIC_FIXTURE_ALLOWLIST = {
    "tests/fixtures/public-plugin-secret-sentinels.json",
}
BINARY_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".webp"}
MAX_ARCHIVE_MEMBERS = 512
MAX_ARCHIVE_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100.0
URL_PATTERN = re.compile(r"https?://[^\\\s'\"<>()]+", re.IGNORECASE)
DOCUMENTATION_HOST_SUFFIXES = (
    ".invalid",
    ".test",
    ".example",
    ".example.com",
    ".example.net",
    ".example.org",
)
DOCUMENTATION_HOSTS = {
    "example.com",
    "example.net",
    "example.org",
    "github.com",
    "developercertificate.org",
    "hermes-agent.nousresearch.com",
    "json-schema.org",
    "pypi.org",
    "files.pythonhosted.org",
}
PRODUCTION_PATTERNS = (
    ("OpenAI-shaped credential", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("NVIDIA-shaped credential", re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub-shaped credential", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")),
    (
        "Bearer credential",
        re.compile(r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/-]{20,}", re.IGNORECASE),
    ),
    (
        "Hermes API credential assignment",
        re.compile(
            r"(?<![A-Za-z0-9_])HERMES_[\"'bBrRuUfF+\s]{0,32}API_"
            r"[\"'bBrRuUfF+\s]{0,32}KEY(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    ),
    ("private key material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)

# Exact-file exemptions are permitted only for synthetic adversarial tests. A byte
# change invalidates the exemption, so adding or replacing any credential-shaped
# value fails closed. Values are populated only after the release candidate is frozen.
SYNTHETIC_FILE_SHA256_ALLOWLIST: dict[str, frozenset[str]] = {
    "README.md": frozenset(
        {
            "8510e5bbf41a3e79a6d5fee1f7816db734307fe7e2a7786b6a61c1ba280520a5",
            "8793ef3bbab749be6e1089034ba627d24c8b733ffd4311951b590dae01d2ca02",
            "c8eb7c5157ac7027dbf6ae86235e63dea6a2dd1faab46f0cbcbd536fbdd21ecf",
        }
    ),
    "scripts/benchmark_migration.py": frozenset(
        {"ef57926174b22c2c22e73843a08a5ea9b7b45481bf51ef08dac21129ea9c929a"}
    ),
    "tests/fixtures/credential_redaction_vectors.json": frozenset(
        {"0cf55fa5cf91acdc164f2eb6936eb49af19ed9c432d060c475db4a9090cd169b"}
    ),
    "tests/test_history.py": frozenset(
        {"3997fc5c3b538df4ded9e994781fd8384b6b3b42ac695e1f1450ac24f6a92e7c"}
    ),
    "tests/test_history_replay.py": frozenset(
        {"87272a47ab814803a77a39cafeb1b193b48678bc889b49712b11bace8b7d8c87"}
    ),
    "tests/test_hardening.py": frozenset(
        {"91e53ca9b16ed43ebd49e7196bd6613f24c03cbf356f2bfc0ea03798cb065753"}
    ),
    "tests/test_memory_provider.py": frozenset(
        {"c2edfbbe5a6320088f58db89b1880200b3e844ed3780885a02ef62d3218ab2b1"}
    ),
    "tests/test_migration_baseline.py": frozenset(
        {"0a3c7af6a761a1b22c5b94f7e1738d5d420e5f085f775d792728ed9ca684ac13"}
    ),
    "tests/test_packaging.py": frozenset(
        {"c7a8e84d116319e62b0b7817c1a049225088c8b3baf59c42ad82cbefc3c172b5"}
    ),
    "tests/test_publication_scanner.py": frozenset(
        {
            "c2352cc593ab08d452479f9ed59651e95acdf0cd5511a94f3830e7910bf65a21",
            "31217ebf89fbd0f13e989a611f46ce5d57a8a384f671d09bb025df2021a8d952",
            "bc7aacce24c8e18f13fa822b02e82431e8f1ae60ac00fffd4fe6bab4738197d4",
            "5b2a6acc6bf7b0ab170a58113ed62ddabb54edb8de8cdb543198346238eb42a3",
            "c68d32a864ded7a7ffc76e3037bbc3a5cf0965c980cae24094995e97e95a63d5",
            "5cb25cc2c84f8aab3de8532141aee25eb0ca5ae3ce8594fc0fb146cf46979f50",
        }
    ),
}

# Legitimate code and documentation may name the protected configuration key.
# These are exact reviewed bytes only; any modification fails closed.
EXACT_PROTECTED_KEY_REFERENCE_SHA256_ALLOWLIST: dict[str, frozenset[str]] = {
    "SECURITY.md": frozenset(
        {
            "34a9cd6cd747589e1ff34fe412d29a04ecc076df19871eaea8a6a078d0b17e06",
            "bb4ba0542582cf3a89a37906651074c52bd1c28754b079bd67fd856c7fe24362",
        }
    ),
    "docs/api-ownership.json": frozenset(
        {"3112d0d252749b45c6e081c6d7987af2c7aa665936ee37eadab2ea7d01f4eb74"}
    ),
    "docs/operation.md": frozenset(
        {"e909870be827f96c4d909641ef1c834a7abad1e71f5c91b17fb8034a1900e4bd"}
    ),
    "docs/public-boundary.json": frozenset(
        {"a54d40239c5b7e3d421b3c70e6cb117dcdf67d6c018370c7321191c1391d8efe"}
    ),
    "docs/public-boundary.md": frozenset(
        {"8b62bb6463fc69c1aa8b56748df77567387c4f553ab7e7aba06617b1e43bee4f"}
    ),
    "legacy-assets/1.2.0/install_hermes_plugin.py": frozenset(
        {"bb9a8483d3d623528f573593eacebffa9483f52ecf84a452c01fa9c362b6879e"}
    ),
    "legacy-assets/1.2.0/substrate_wiki.zip": frozenset(
        {"2cbf504ec83352f23a1157777d24272b62e4b7300ad0ca991a0c4bc2e2df30b5"}
    ),
    "legacy-assets/1.3.0/install_hermes_plugin.py": frozenset(
        {"59d4d0b8557a49ec18160f4245a533465f8c4c0eb344d235af155afb8845d1b1"}
    ),
    "legacy-assets/1.3.0/substrate_wiki.zip": frozenset(
        {"6827c00444c799c085ac7a3669721d672c0d7e1e703a8a491397bb16d0655c02"}
    ),
    "legacy-assets/1.4.0/install_hermes_plugin.py": frozenset(
        {"13a05be49a83fab4c75171356d575dd85e00b27b8e09ce1602b87ae903741608"}
    ),
    "legacy-assets/1.4.0/substrate_wiki.zip": frozenset(
        {"df872d60dfc53668a0e6d30fd024e8d2f533306375980c3815ef1a483676c667"}
    ),
    "legacy-assets/1.4.1/install_hermes_plugin.py": frozenset(
        {"7600b2681c3aebcb1b1492b0a04be38bbbec637089cbbcfb1cc26e8c10865b8d"}
    ),
    "legacy-assets/1.4.1/substrate_wiki.zip": frozenset(
        {"877ccf9b0212792b699d9c98912a26980675a6050df3bd319e927639e3d901f1"}
    ),
    "scripts/install_hermes_plugin.py": frozenset(
        {
            "21ee987d4136187f020ce33ded442a9b20e97e380a2f77d4e01f6b6f1fb95617",
            "95de10e9c4e9c37c7bd3bcce1f8b507de977682e869ec4d44386f197a910554f",
        }
    ),
    "scripts/verify_public_plugin_candidate.py": frozenset(
        {
            "05e92b524fd1d08d0ff6946d9daa1fafa57c31dfe7905728d8455af88cc16759",
            "215bd6f7dab95220d03a61b998807b6ea9d18ee4f54e7b215587f40befa662ce",
            "7f650fdf6f464ba1a1698b8bb54868159823e2a17f147a1c1c43f5810464facf",
            "a421abde8bd5f3413dbd36bfc77adae6a5018ca5c62f81c94887035589e67aee",
            "eb8199baa3626770e7faf620cc73fb46022bca98f425631aff423ef8c008de27",
        }
    ),
    "src/substrate_wiki/README.md": frozenset(
        {"8612ab5974a8a5be18b2583043a7853317c9eddc7801d726d83f71220c848796"}
    ),
    "src/substrate_wiki/__init__.py": frozenset(
        {"a4143022e05a7b93d3aa5799f4319601292e67069ad85d172770c3effe0e5e9d"}
    ),
    "src/substrate_wiki/client.py": frozenset(
        {"c711b64e496214de3acf5639075bd6bada19b687cd30426058e1b0bb443dce0b"}
    ),
    "src/substrate_wiki/redaction.py": frozenset(
        {"e9bec198aa7ad018da359d2e9aa6df1dab717881bb41b1001348911b23e6439b"}
    ),
}

# This digest freezes the independently reviewed source/destination/class boundary
# separately from per-file hashes in the mutable extraction manifest. It covers
# only that sorted policy projection, so ordinary byte changes do not change
# policy. Adding, moving, or reclassifying a file requires explicit review.
TRUSTED_INVENTORY_POLICY_SHA256 = (
    "2c93f43f7565b8b347097416543f4210ee9b1f8e79247ea06b2a97b8074a5e52"
)
TRUSTED_HISTORICAL_BLOB_POLICY_SHA256 = (
    "0000000000000000000000000000000000000000000000000000000000000000"
)
SCANNER_PATH = "scripts/verify_public_plugin_candidate.py"
DESTINATION_MANIFEST_PATH = "docs/extraction-manifest.json"


def _normalize_historical_policy_payload(relative_path: str, payload: bytes) -> bytes:
    """Remove the history policy's intentional self-reference before hashing."""

    if relative_path == SCANNER_PATH:
        text = payload.decode("utf-8")
        text, count = re.subn(
            r'(?s)(TRUSTED_HISTORICAL_BLOB_POLICY_SHA256\s*=\s*\(\s*")[0-9a-f]{64}("\s*\))',
            lambda match: match.group(1) + "0" * 64 + match.group(2),
            text,
            count=1,
        )
        return text.encode("utf-8") if count else payload
    if relative_path == DESTINATION_MANIFEST_PATH:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return payload
        normalized = 0
        for item in value.get("entries", []):
            if isinstance(item, dict) and item.get("destination") == SCANNER_PATH:
                item["destination_sha256"] = "0" * 64
                normalized += 1
        for item in value.get("destination_only", []):
            if isinstance(item, dict) and item.get("path") == SCANNER_PATH:
                item["sha256"] = "0" * 64
                normalized += 1
        if normalized == 1:
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return payload


def _publication_tree_roots(root: Path) -> list[tuple[str, str]]:
    """Return every commit tree and every ref recursively peeled to a tree."""

    roots = {
        (f"commit:{commit}", commit)
        for commit in _git(root, "rev-list", "--all").decode("ascii").splitlines()
    }
    refs = _git(root, "for-each-ref", "--format=%(refname)").decode("utf-8").splitlines()
    for ref in refs:
        object_id = _git(root, "rev-parse", f"{ref}^{{}}").decode("ascii").strip()
        object_type = _git(root, "cat-file", "-t", object_id).decode("ascii").strip()
        if object_type == "tree":
            roots.add((f"tree:{object_id}", object_id))
    return sorted(roots)


def _historical_blob_policy_sha256(root: Path, manifest: dict[str, Any]) -> str:
    """Hash every distinct path/class/mode/content tuple across published refs."""

    path_classes = {
        item["destination"]: item["class"] for item in manifest["entries"]
    }
    path_classes.update(
        {item["path"]: item["class"] for item in manifest["destination_only"]}
    )
    path_classes[manifest["self_excluded_path"]] = "closed-inventory-manifest"
    projection: set[tuple[str, str, str, str]] = set()
    for _, treeish in _publication_tree_roots(root):
        for raw_entry in (
            entry for entry in _git(root, "ls-tree", "-r", "-z", treeish).split(b"\0") if entry
        ):
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, raw_object_id = metadata.split(b" ", 2)
            relative_path = raw_path.decode("utf-8")
            object_id = raw_object_id.decode("ascii")
            if object_type == b"blob":
                payload = _git(root, "cat-file", "blob", object_id)
                normalized_digest = hashlib.sha256(
                    _normalize_historical_policy_payload(relative_path, payload)
                ).hexdigest()
            else:
                normalized_digest = object_id
            projection.add(
                (
                    relative_path,
                    path_classes.get(relative_path, "outside-closed-inventory"),
                    mode.decode("ascii"),
                    f"{object_type.decode('ascii')}:{normalized_digest}",
                )
            )
    payload = json.dumps(sorted(projection), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _resolved_python_strings(text: str) -> set[str]:
    """Resolve bounded constant string assignments without executing candidate code."""

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    assignments = sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))),
        key=lambda node: (getattr(node, "lineno", 0), getattr(node, "col_offset", 0)),
    )
    values: dict[str, str] = {}

    def resolve(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = resolve(node.left)
            right = resolve(node.right)
            if left is not None and right is not None and len(left) + len(right) <= 256:
                return left + right
        return None

    for _ in range(min(len(assignments) + 1, 64)):
        changed = False
        for assignment in assignments:
            value = resolve(assignment.value)
            if value is None:
                continue
            targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
            for target in targets:
                if isinstance(target, ast.Name) and values.get(target.id) != value:
                    values[target.id] = value
                    changed = True
        if not changed:
            break
    return set(values.values())


def scan_text(relative_path: str, text: str) -> list[str]:
    """Return content-free findings for one candidate text file."""

    findings: list[str] = []
    sentinels = SYNTHETIC_SENTINEL_PATTERN.findall(text)
    sentinel_candidates = SYNTHETIC_SENTINEL_CANDIDATE_PATTERN.findall(text)
    if len(sentinel_candidates) != len(sentinels):
        findings.append(f"{relative_path}: malformed synthetic sentinel")
    if sentinels and relative_path not in SYNTHETIC_FIXTURE_ALLOWLIST:
        findings.append(f"{relative_path}: synthetic sentinel outside exact fixture allowlist")
    production_text = SYNTHETIC_SENTINEL_PATTERN.sub("[SYNTHETIC-SENTINEL]", text)
    production_text = re.sub(
        r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))",
        lambda match: chr(int(match.group(1) or match.group(2), 16))
        if int(match.group(1) or match.group(2), 16) < 128
        else match.group(0),
        production_text,
    )
    for label, pattern in PRODUCTION_PATTERNS:
        if pattern.search(production_text):
            findings.append(f"{relative_path}: {label}")
    if any(value.casefold() == "hermes_api_key" for value in _resolved_python_strings(text)):
        findings.append(f"{relative_path}: Hermes API credential assignment")
    for value in URL_PATTERN.findall(production_text):
        try:
            hostname = (urlsplit(value).hostname or "").casefold().rstrip(".")
        except ValueError:
            findings.append(f"{relative_path}: production-shaped HTTP(S) endpoint")
            continue
        allowed = (
            hostname in {"localhost", "127.0.0.1", "::1"}
            or hostname in DOCUMENTATION_HOSTS
            or hostname.endswith(DOCUMENTATION_HOST_SUFFIXES)
        )
        if not allowed:
            findings.append(f"{relative_path}: production-shaped HTTP(S) endpoint")
    return findings


def scan_archive(relative_path: str, archive: Path) -> list[str]:
    """Inspect every bounded UTF-8 member of a ZIP candidate."""

    findings: list[str] = []
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                return [f"{relative_path}: archive member count exceeds bound"]
            total_bytes = 0
            for member in members:
                member_path = PurePosixPath(member.filename)
                target = f"{relative_path}!/{member.filename}"
                if member.flag_bits & 0x1:
                    findings.append(f"{target}: encrypted archive member")
                    continue
                if member_path.is_absolute() or ".." in member_path.parts:
                    findings.append(f"{target}: unsafe archive path")
                    continue
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    findings.append(f"{target}: archive symlink is forbidden")
                    continue
                if member.is_dir():
                    continue
                total_bytes += member.file_size
                if total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                    return sorted(findings + [f"{relative_path}: archive size exceeds bound"])
                if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    findings.append(f"{target}: archive member size exceeds bound")
                    continue
                ratio = member.file_size / max(member.compress_size, 1)
                if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                    findings.append(f"{target}: archive compression ratio exceeds bound")
                    continue
                try:
                    payload = bundle.read(member)
                except RuntimeError:
                    findings.append(f"{target}: unreadable archive member")
                    continue
                if member_path.suffix.casefold() in BINARY_SUFFIXES:
                    text = payload.decode("latin-1")
                else:
                    try:
                        text = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        findings.append(f"{target}: unexpected non-UTF-8 archive member")
                        text = payload.decode("latin-1")
                findings.extend(scan_text(target, text))
    except (OSError, zipfile.BadZipFile):
        return [f"{relative_path}: invalid ZIP archive"]
    return sorted(findings)


def _scan_bytes(relative_path: str, payload: bytes) -> list[str]:
    """Scan one ordinary file or bounded ZIP payload."""

    digest = hashlib.sha256(payload).hexdigest()
    allowed_digests = SYNTHETIC_FILE_SHA256_ALLOWLIST.get(
        relative_path, frozenset()
    ) | EXACT_PROTECTED_KEY_REFERENCE_SHA256_ALLOWLIST.get(relative_path, frozenset())
    if digest in allowed_digests:
        return []
    if Path(relative_path).suffix.casefold() == ".zip":
        with tempfile.NamedTemporaryFile(suffix=".zip") as stream:
            stream.write(payload)
            stream.flush()
            return scan_archive(relative_path, Path(stream.name))
    if Path(relative_path).suffix.casefold() in BINARY_SUFFIXES:
        text = payload.decode("latin-1")
    else:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return [f"{relative_path}: unexpected non-UTF-8 candidate file"]
    return scan_text(relative_path, text)


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ("git", "-C", str(root), *args),
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("candidate Git inventory is unavailable") from exc


def _nul_paths(payload: bytes) -> list[str]:
    return [value.decode("utf-8") for value in payload.split(b"\0") if value]


def _all_candidate_paths(root: Path) -> set[str]:
    """Enumerate every file/symlink outside Git metadata, including ignored files."""

    paths: set[str] = set()
    for directory, names, files in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        if directory_path == root and ".git" in names:
            names.remove(".git")
        for name in (*names, *files):
            path = directory_path / name
            if path.is_file() or path.is_symlink():
                paths.add(path.relative_to(root).as_posix())
    return paths


def _scan_destination_repository(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Scan the closed working tree plus every blob reachable from a Git ref."""

    findings: list[str] = []
    try:
        inventory_policy = {
            "entries": sorted(
                (
                    {
                        "source": item["source"],
                        "destination": item["destination"],
                        "class": item["class"],
                    }
                    for item in manifest["entries"]
                ),
                key=lambda item: item["destination"],
            ),
            "destination_only": sorted(
                (
                    {"path": item["path"], "class": item["class"]}
                    for item in manifest["destination_only"]
                ),
                key=lambda item: item["path"],
            ),
        }
        policy_payload = json.dumps(
            inventory_policy, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (KeyError, TypeError):
        findings.append("candidate: invalid closed inventory path/class policy")
    else:
        if hashlib.sha256(policy_payload).hexdigest() != TRUSTED_INVENTORY_POLICY_SHA256:
            findings.append("candidate: closed inventory path/class policy mismatch")
    tracked = set(_nul_paths(_git(root, "ls-files", "-z")))
    unexpected = _all_candidate_paths(root) - tracked
    for relative_path in sorted(unexpected):
        findings.append(f"{relative_path}: unexpected untracked candidate file")
        path = root / relative_path
        if path.is_symlink() or not path.is_file():
            findings.append(f"{relative_path}: unsafe or missing untracked file")
            continue
        findings.extend(_scan_bytes(relative_path, path.read_bytes()))

    sources: set[str] = set()
    destinations: set[str] = set()
    for item in manifest["entries"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("source"), str)
            or not isinstance(item.get("destination"), str)
        ):
            raise ValueError("invalid destination inventory entry")
        source_path = item["source"]
        relative_path = item["destination"]
        if source_path in sources:
            findings.append(f"{source_path}: duplicate source inventory entry")
        if relative_path in destinations:
            findings.append(f"{relative_path}: duplicate destination inventory entry")
        sources.add(source_path)
        destinations.add(relative_path)
        expected_digest = item.get("destination_sha256")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            findings.append(f"{relative_path}: missing destination SHA-256")
        elif (root / relative_path).is_file() and hashlib.sha256(
            (root / relative_path).read_bytes()
        ).hexdigest() != expected_digest:
            findings.append(f"{relative_path}: destination SHA-256 mismatch")

    destination_only = manifest.get("destination_only")
    if not isinstance(destination_only, list):
        raise ValueError("destination-only inventory is missing")
    destination_only_paths: set[str] = set()
    for item in destination_only:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("invalid destination-only inventory entry")
        relative_path = item["path"]
        if relative_path in destination_only_paths:
            findings.append(f"{relative_path}: duplicate destination-only inventory entry")
        if relative_path in destinations:
            findings.append(f"{relative_path}: path appears in both destination inventories")
        destination_only_paths.add(relative_path)
        expected_digest = item.get("sha256")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            findings.append(f"{relative_path}: missing destination-only SHA-256")
        elif (root / relative_path).is_file() and hashlib.sha256(
            (root / relative_path).read_bytes()
        ).hexdigest() != expected_digest:
            findings.append(f"{relative_path}: destination-only SHA-256 mismatch")

    self_path = manifest.get("self_excluded_path")
    if not isinstance(self_path, str):
        raise ValueError("manifest self-exclusion is missing")
    expected_tracked = destinations | destination_only_paths | {self_path}
    findings.extend(
        f"{path}: tracked candidate path is absent from closed inventory"
        for path in sorted(tracked - expected_tracked)
    )
    findings.extend(
        f"{path}: closed inventory path is not tracked"
        for path in sorted(expected_tracked - tracked)
    )

    for relative_path in sorted(tracked):
        path = root / relative_path
        if path.is_symlink() or not path.is_file():
            findings.append(f"{relative_path}: unsafe or missing tracked file")
            continue
        findings.extend(_scan_bytes(relative_path, path.read_bytes()))

    tree_blob_ids: set[str] = set()
    for root_label, treeish in _publication_tree_roots(root):
        tree_entries = _git(root, "ls-tree", "-r", "-z", treeish).split(b"\0")
        for raw_entry in (entry for entry in tree_entries if entry):
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, raw_object_id = metadata.split(b" ", 2)
            relative_path = raw_path.decode("utf-8")
            object_id = raw_object_id.decode("ascii")
            tree_blob_ids.add(object_id)
            if relative_path not in expected_tracked:
                findings.append(
                    f"git:{root_label}:{relative_path}: historical path outside closed inventory"
                )
            if object_type != b"blob" or mode not in {b"100644", b"100755"}:
                findings.append(
                    f"git:{root_label}:{relative_path}: historical entry is not a regular file"
                )
            for finding in scan_text(f"git-path:{root_label}", relative_path):
                _, label = finding.rsplit(": ", 1)
                findings.append(f"git:{root_label}:{relative_path}: {label}")
            if object_type != b"blob":
                continue
            payload = _git(root, "cat-file", "blob", object_id)
            for finding in _scan_bytes(relative_path, payload):
                _, label = finding.split(": ", 1)
                findings.append(f"git:{root_label}:{relative_path}: {label}")

    try:
        historical_policy_digest = _historical_blob_policy_sha256(root, manifest)
    except (KeyError, TypeError, ValueError):
        findings.append("candidate: invalid historical blob policy")
    else:
        if historical_policy_digest != TRUSTED_HISTORICAL_BLOB_POLICY_SHA256:
            findings.append("candidate: historical blob policy mismatch")

    reachable_objects = {
        line.split(b" ", 1)[0].decode("ascii")
        for line in _git(root, "rev-list", "--objects", "--all").splitlines()
        if line
    }
    for object_id in sorted(reachable_objects):
        object_type = _git(root, "cat-file", "-t", object_id).decode("ascii").strip()
        if object_type not in {"blob", "commit", "tag"}:
            continue
        if object_type == "blob" and object_id in tree_blob_ids:
            continue
        payload = _git(root, "cat-file", object_type, object_id)
        for finding in _scan_bytes(f"git-object:{object_id}:{object_type}.txt", payload):
            _, label = finding.rsplit(": ", 1)
            findings.append(f"git-object:{object_id}:{object_type}: {label}")
    return sorted(set(findings))


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise ValueError("invalid public plugin move manifest")
    return value


def scan_candidate(
    root: Path,
    manifest_path: Path,
    *,
    layout: Literal["auto", "source", "destination"] = "auto",
) -> list[str]:
    """Scan the closed candidate repository or the exact embedded source manifest."""

    manifest = _load_manifest(manifest_path)
    findings: list[str] = []
    seen: set[str] = set()
    entries = manifest["entries"]
    if layout == "auto":
        if all(
            isinstance(item, dict) and (root / str(item.get("source"))).is_file()
            for item in entries
        ):
            selected_layout = "source"
        elif all(
            isinstance(item, dict) and (root / str(item.get("destination"))).is_file()
            for item in entries
        ):
            selected_layout = "destination"
        else:
            return ["candidate: neither source nor destination layout is complete"]
    else:
        selected_layout = layout
    if selected_layout == "destination":
        return _scan_destination_repository(root, manifest)
    for item in entries:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("source"), str)
            or not isinstance(item.get("destination"), str)
        ):
            raise ValueError("invalid public plugin move entry")
        relative_path = item[selected_layout]
        if relative_path in seen:
            raise ValueError("duplicate public plugin move source")
        seen.add(relative_path)
        source = root / relative_path
        if not source.is_file():
            findings.append(f"{relative_path}: missing manifest source")
            continue
        expected_digest = item.get("source_sha256")
        if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            findings.append(f"{relative_path}: missing source SHA-256")
        elif hashlib.sha256(source.read_bytes()).hexdigest() != expected_digest:
            findings.append(f"{relative_path}: source SHA-256 mismatch")
        findings.extend(_scan_bytes(relative_path, source.read_bytes()))
    return sorted(findings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--layout", choices=("auto", "source", "destination"), default="auto")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.manifest is None:
        candidates = (
            root / "docs" / "extraction-manifest.json",
            root / "docs" / "file-move-manifest.json",
        )
        manifest = next(
            (candidate for candidate in candidates if candidate.is_file()), candidates[0]
        )
    else:
        manifest = args.manifest if args.manifest.is_absolute() else root / args.manifest
    findings = scan_candidate(root, manifest, layout=args.layout)
    print(json.dumps({"findings": findings, "status": "pass" if not findings else "fail"}))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
