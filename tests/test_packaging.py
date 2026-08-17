from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).parents[1]
BUILDER_PATH = REPOSITORY_ROOT / "scripts" / "build_plugin.py"
INSTALLER_PATH = REPOSITORY_ROOT / "scripts" / "install_hermes_plugin.py"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
PLUGIN_SOURCE = REPOSITORY_ROOT / "src" / "substrate_wiki"
LEGACY_RELEASE_120 = REPOSITORY_ROOT / "legacy-assets" / "1.2.0"
LEGACY_RELEASE_130 = REPOSITORY_ROOT / "legacy-assets" / "1.3.0"
LEGACY_RELEASE_140 = REPOSITORY_ROOT / "legacy-assets" / "1.4.0"
LEGACY_RELEASE_141 = REPOSITORY_ROOT / "legacy-assets" / "1.4.1"
LEGACY_ARCHIVE_SHA256 = "2cbf504ec83352f23a1157777d24272b62e4b7300ad0ca991a0c4bc2e2df30b5"
LEGACY_INSTALLER_SHA256 = "bb9a8483d3d623528f573593eacebffa9483f52ecf84a452c01fa9c362b6879e"
LEGACY_130_ARCHIVE_SHA256 = "6827c00444c799c085ac7a3669721d672c0d7e1e703a8a491397bb16d0655c02"
LEGACY_130_INSTALLER_SHA256 = "59d4d0b8557a49ec18160f4245a533465f8c4c0eb344d235af155afb8845d1b1"
LEGACY_140_ARCHIVE_SHA256 = "df872d60dfc53668a0e6d30fd024e8d2f533306375980c3815ef1a483676c667"
LEGACY_140_INSTALLER_SHA256 = "13a05be49a83fab4c75171356d575dd85e00b27b8e09ce1602b87ae903741608"
LEGACY_141_ARCHIVE_SHA256 = "877ccf9b0212792b699d9c98912a26980675a6050df3bd319e927639e3d901f1"
LEGACY_141_INSTALLER_SHA256 = "7600b2681c3aebcb1b1492b0a04be38bbbec637089cbbcfb1cc26e8c10865b8d"
EXPECTED_MEMBERS = [
    'substrate_wiki/',
    'substrate_wiki/PROVENANCE.json',
    'substrate_wiki/LICENSE',
    'substrate_wiki/README.md',
    'substrate_wiki/__init__.py',
    'substrate_wiki/checkpoint.py',
    'substrate_wiki/cli.py',
    'substrate_wiki/client.py',
    'substrate_wiki/credentials.py',
    'substrate_wiki/events.py',
    'substrate_wiki/history.py',
    'substrate_wiki/onboarding.py',
    'substrate_wiki/plugin.yaml',
    'substrate_wiki/py.typed',
    'substrate_wiki/redaction.py',
    'substrate_wiki/spool.py',
    'substrate_wiki/supervisor.py',
    'substrate_wiki/worker.py',
]


def load_builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_plugin", BUILDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_installer() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_hermes_plugin", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_identity_matches_direct_install_package() -> None:
    manifest = (PLUGIN_SOURCE / "plugin.yaml").read_text(encoding="utf-8")

    assert "name: substrate_wiki\n" in manifest
    assert "name: substrate-wiki" not in manifest


def test_packaged_readme_uses_the_standalone_release_installer() -> None:
    readme = (PLUGIN_SOURCE / "README.md").read_text(encoding="utf-8")

    assert "python3 install_hermes_plugin.py" in readme
    assert "python scripts/install_hermes_plugin.py" not in readme


def test_root_readme_keeps_published_release_state_truthful() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
    boundary = json.loads(
        (REPOSITORY_ROOT / "docs" / "public-boundary.json").read_text(encoding="utf-8")
    )

    assert boundary["repository"]["candidate_source_of_truth"] is False
    assert boundary["repository"]["source_of_truth"] is True
    assert boundary["legal"]["status"] == "published"
    assert "canonical editable source" in readme
    assert "`v2.0.3` is not published yet" not in readme
    assert "releases/download/v2.0.3" in readme
    assert "dfaa786f68dd819e1313191bb26253caf6bc52fe4b0ab4f6f8c2e2ebcb62e1a3" in readme


def test_release_workflow_keeps_dependency_execution_out_of_privileged_publisher() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    verify, publish = workflow.split("\n  publish:\n", maxsplit=1)

    assert "github.ref == 'refs/heads/main' && github.sha == inputs.candidate_sha" in verify
    assert "persist-credentials: false" in verify
    assert "uv sync --frozen --extra dev --no-install-project" in verify
    assert "uv run --no-sync" in verify
    assert "contents: write" not in verify
    assert "id-token: write" not in verify
    assert "attestations: write" not in verify

    assert "environment: public-release" in publish
    assert "github.ref == 'refs/heads/main' && github.sha == inputs.candidate_sha" in publish
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in publish
    assert "contents: write" in publish
    assert "id-token: write" in publish
    assert "attestations: write" in publish
    assert "actions/checkout@" not in publish
    assert "uv sync" not in publish
    assert "uv run" not in publish
    assert "setuptools" not in publish


def test_archive_is_canonical_and_release_clean(tmp_path: Path) -> None:
    builder = load_builder()
    archive_path = tmp_path / "substrate_wiki.zip"
    archive_path.write_bytes(builder.build_archive_bytes())

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == EXPECTED_MEMBERS
        for info in archive.infolist():
            assert info.date_time == builder.FIXED_TIMESTAMP
            assert info.create_system == 3
            assert "__pycache__" not in info.filename
            assert not info.filename.endswith((".pyc", ".pyo"))
        assert archive.read("substrate_wiki/LICENSE") == (REPOSITORY_ROOT / "LICENSE").read_bytes()
        for member in EXPECTED_MEMBERS[3:]:
            source_path = PLUGIN_SOURCE / Path(member).relative_to("substrate_wiki")
            assert archive.read(member) == source_path.read_bytes()


def test_archive_provenance_identifies_version_and_source_hashes(tmp_path: Path) -> None:
    builder = load_builder()
    archive_path = tmp_path / "substrate_wiki.zip"
    archive_path.write_bytes(builder.build_archive_bytes())

    with zipfile.ZipFile(archive_path) as archive:
        provenance = json.loads(archive.read("substrate_wiki/PROVENANCE.json"))

    assert provenance == {
        "build_format_version": 3,
        "license_sha256": hashlib.sha256((REPOSITORY_ROOT / "LICENSE").read_bytes()).hexdigest(),
        "plugin_version": "2.0.3",
        "provider_id": "substrate_wiki",
        "source_commit": "unknown",
        "source_files": {
            filename: hashlib.sha256((PLUGIN_SOURCE / filename).read_bytes()).hexdigest()
            for filename in builder.REQUIRED_FILES
        },
        "target_hermes_version": "0.20.0",
    }


def test_builder_is_deterministic() -> None:
    builder = load_builder()

    assert builder.build_archive_bytes() == builder.build_archive_bytes()


def test_v120_release_artifacts_remain_byte_pinned() -> None:
    assert (
        hashlib.sha256((LEGACY_RELEASE_120 / "substrate_wiki.zip").read_bytes()).hexdigest()
        == LEGACY_ARCHIVE_SHA256
    )
    assert (
        hashlib.sha256((LEGACY_RELEASE_120 / "install_hermes_plugin.py").read_bytes()).hexdigest()
        == LEGACY_INSTALLER_SHA256
    )


def test_v130_release_artifacts_remain_byte_pinned() -> None:
    assert (
        hashlib.sha256((LEGACY_RELEASE_130 / "substrate_wiki.zip").read_bytes()).hexdigest()
        == LEGACY_130_ARCHIVE_SHA256
    )
    assert (
        hashlib.sha256((LEGACY_RELEASE_130 / "install_hermes_plugin.py").read_bytes()).hexdigest()
        == LEGACY_130_INSTALLER_SHA256
    )


def test_v140_release_artifacts_remain_byte_pinned() -> None:
    assert (
        hashlib.sha256((LEGACY_RELEASE_140 / "substrate_wiki.zip").read_bytes()).hexdigest()
        == LEGACY_140_ARCHIVE_SHA256
    )
    assert (
        hashlib.sha256(
            (LEGACY_RELEASE_140 / "install_hermes_plugin.py").read_bytes()
        ).hexdigest()
        == LEGACY_140_INSTALLER_SHA256
    )


def test_v141_release_artifacts_remain_byte_pinned() -> None:
    assert (
        hashlib.sha256((LEGACY_RELEASE_141 / "substrate_wiki.zip").read_bytes()).hexdigest()
        == LEGACY_141_ARCHIVE_SHA256
    )
    assert (
        hashlib.sha256(
            (LEGACY_RELEASE_141 / "install_hermes_plugin.py").read_bytes()
        ).hexdigest()
        == LEGACY_141_INSTALLER_SHA256
    )


def test_v140_event_envelope_constants_remain_stream_v2() -> None:
    # events.py now contains the mandatory stateful cross-chunk redactor, so it
    # cannot remain byte-identical. The public capture/retention limits remain
    # locked and the replay suite verifies deterministic v2 IDs and boundaries.
    source = (PLUGIN_SOURCE / "events.py").read_text(encoding="utf-8")
    assert 'PROVIDER_ID = "substrate_wiki"' in source
    assert "SCHEMA_VERSION = 2" in source
    assert "RETENTION_DAYS = 90" in source
    assert "MAX_CAPTURE_BYTES = 256 * 1024" in source
    assert "capture_chunk" in source


def test_publish_release_creates_exact_current_and_immutable_aliases(tmp_path: Path) -> None:
    builder = load_builder()
    archive_path = tmp_path / "current" / "substrate_wiki.zip"
    releases_path = tmp_path / "releases"
    archive = builder.build_archive_bytes(source_commit="a" * 40)

    release_archive, release_installer = builder.publish_release(
        archive,
        archive_path=archive_path,
        releases_path=releases_path,
    )

    assert release_archive == releases_path / "2.0.3" / "substrate_wiki.zip"
    assert release_installer == releases_path / "2.0.3" / "install_hermes_plugin.py"
    assert archive_path.read_bytes() == release_archive.read_bytes() == archive
    assert release_installer.read_bytes() == INSTALLER_PATH.read_bytes()
    assert builder.check_release(
        archive_path=archive_path,
        releases_path=releases_path,
    )


def test_publish_release_refuses_to_replace_versioned_bytes(tmp_path: Path) -> None:
    builder = load_builder()
    releases_path = tmp_path / "releases"
    versioned = releases_path / "2.0.3" / "substrate_wiki.zip"
    versioned.parent.mkdir(parents=True)
    versioned.write_bytes(b"different immutable bytes")
    archive_path = tmp_path / "current" / "substrate_wiki.zip"

    with pytest.raises(ValueError, match="immutable release artifact differs"):
        builder.publish_release(
            builder.build_archive_bytes(source_commit="a" * 40),
            archive_path=archive_path,
            releases_path=releases_path,
        )

    assert versioned.read_bytes() == b"different immutable bytes"
    assert not archive_path.exists()


def test_publish_release_preflights_both_immutable_artifacts(tmp_path: Path) -> None:
    builder = load_builder()
    releases_path = tmp_path / "releases"
    versioned_installer = releases_path / "2.0.3" / "install_hermes_plugin.py"
    versioned_installer.parent.mkdir(parents=True)
    versioned_installer.write_bytes(b"conflicting immutable installer")
    archive_path = tmp_path / "current" / "substrate_wiki.zip"

    with pytest.raises(ValueError, match="immutable release artifact differs"):
        builder.publish_release(
            builder.build_archive_bytes(source_commit="a" * 40),
            archive_path=archive_path,
            releases_path=releases_path,
        )

    assert not (releases_path / "2.0.3" / "substrate_wiki.zip").exists()
    assert not archive_path.exists()


def test_publish_release_rejects_symlinked_immutable_artifact(tmp_path: Path) -> None:
    builder = load_builder()
    releases_path = tmp_path / "releases"
    release_directory = releases_path / "2.0.3"
    release_directory.mkdir(parents=True)
    target = tmp_path / "elsewhere.zip"
    archive = builder.build_archive_bytes(source_commit="a" * 40)
    target.write_bytes(archive)
    versioned = release_directory / "substrate_wiki.zip"
    try:
        versioned.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="artifact path is unsafe"):
        builder.publish_release(
            archive,
            archive_path=tmp_path / "current" / "substrate_wiki.zip",
            releases_path=releases_path,
        )

    assert versioned.is_symlink()
    assert target.read_bytes() == archive


def test_installer_verifies_and_atomically_upgrades_with_rollback(tmp_path: Path) -> None:
    builder = load_builder()
    installer = load_installer()
    archive = tmp_path / "substrate_wiki.zip"
    archive.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    hermes_home = tmp_path / "hermes"
    existing = hermes_home / "plugins" / "substrate_wiki"
    existing.mkdir(parents=True)
    (existing / "plugin.yaml").write_text("name: substrate_wiki\nversion: 1.0.0\n")
    checkpoint = hermes_home / "substrate_wiki" / "imports" / "jobs" / "same-job" / "checkpoint.db"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"content-free-checkpoint")

    result = installer.install(archive, hermes_home, expected_sha256=digest)

    assert result["action"] == "upgraded"
    assert result["source_commit"] == "a" * 40
    assert "version: 2.0.3" in (existing / "plugin.yaml").read_text(encoding="utf-8")
    rollback = Path(result["rollback"])
    assert "version: 1.0.0" in (rollback / "plugin.yaml").read_text(encoding="utf-8")
    assert checkpoint.read_bytes() == b"content-free-checkpoint"
    with pytest.raises(ValueError, match="SHA-256"):
        installer.verify_archive(archive, "0" * 64)


def test_install_refuses_to_mutate_without_a_pinned_sha256(tmp_path: Path) -> None:
    builder = load_builder()
    installer = load_installer()
    archive = tmp_path / "substrate_wiki.zip"
    archive.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    hermes_home = tmp_path / "hermes"

    with pytest.raises(ValueError, match="pinned archive SHA-256 is required"):
        installer.install(archive, hermes_home)

    assert not (hermes_home / "plugins" / "substrate_wiki").exists()


def test_installer_cli_requires_the_pinned_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    monkeypatch.setattr(
        installer.sys,
        "argv",
        [
            "install_hermes_plugin.py",
            "--archive",
            os.fspath(tmp_path / "substrate_wiki.zip"),
            "--yes",
        ],
    )

    with pytest.raises(SystemExit) as error:
        installer.main()

    assert error.value.code == 2


def test_plugin_swap_restores_previous_version_when_hardening_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = load_builder()
    installer = load_installer()
    archive = tmp_path / "substrate_wiki.zip"
    archive.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    hermes_home = tmp_path / "hermes"
    existing = hermes_home / "plugins" / "substrate_wiki"
    existing.mkdir(parents=True)
    (existing / "plugin.yaml").write_text(
        "name: substrate_wiki\nversion: 1.3.0\n", encoding="utf-8"
    )

    def fail_hardening(target: Path) -> None:
        assert "version: 2.0.3" in (target / "plugin.yaml").read_text(encoding="utf-8")
        raise OSError("permission hardening failed")

    monkeypatch.setattr(installer, "_harden_plugin_permissions", fail_hardening)
    with pytest.raises(OSError, match="permission hardening failed"):
        installer.install(archive, hermes_home, expected_sha256=digest)

    assert "version: 1.3.0" in (existing / "plugin.yaml").read_text(encoding="utf-8")
    failed = list((hermes_home / "plugins").glob("substrate_wiki.failed-*"))
    assert len(failed) == 1
    assert "version: 2.0.3" in (failed[0] / "plugin.yaml").read_text(encoding="utf-8")


def test_check_archive_preserves_sha_provenance_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = load_builder()
    archive_path = tmp_path / "substrate_wiki.zip"
    source_commit = "a" * 40

    monkeypatch.setenv(builder.SOURCE_COMMIT_ENVIRONMENT_VARIABLE, source_commit)
    archive_path.write_bytes(builder.build_archive_bytes())
    monkeypatch.delenv(builder.SOURCE_COMMIT_ENVIRONMENT_VARIABLE)

    assert builder.check_archive(archive_path)


def test_check_archive_rejects_malformed_provenance(tmp_path: Path) -> None:
    builder = load_builder()
    archive_path = tmp_path / "substrate_wiki.zip"

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("substrate_wiki/PROVENANCE.json", '{"source_commit": "not-a-sha"}')

    with pytest.raises(ValueError, match="malformed provenance"):
        builder.check_archive(archive_path)


def test_installer_rejects_unlisted_archive_members(tmp_path: Path) -> None:
    builder = load_builder()
    installer = load_installer()
    archive_path = tmp_path / "substrate_wiki.zip"
    archive_path.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    with zipfile.ZipFile(archive_path, "a") as archive:
        archive.writestr("substrate_wiki/unlisted.py", "raise RuntimeError('untrusted')\n")

    with pytest.raises(ValueError, match="unexpected file set"):
        installer.verify_archive(archive_path)


def test_installer_rejects_license_bytes_that_do_not_match_provenance(tmp_path: Path) -> None:
    builder = load_builder()
    installer = load_installer()
    canonical_path = tmp_path / "canonical.zip"
    canonical_path.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    archive_path = tmp_path / "tampered-license.zip"
    with zipfile.ZipFile(canonical_path) as source, zipfile.ZipFile(archive_path, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "substrate_wiki/LICENSE":
                content = b"not the reviewed license\n"
            target.writestr(info, content)

    with pytest.raises(ValueError, match="license digest mismatch"):
        installer.verify_archive(archive_path)


def test_installer_rejects_non_regular_allowlisted_members(tmp_path: Path) -> None:
    builder = load_builder()
    installer = load_installer()
    canonical_path = tmp_path / "canonical.zip"
    canonical_path.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    archive_path = tmp_path / "tampered.zip"
    with zipfile.ZipFile(canonical_path) as source, zipfile.ZipFile(archive_path, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "substrate_wiki/client.py":
                info.external_attr = 0o40755 << 16
            target.writestr(info, content)

    with pytest.raises(ValueError, match="non-regular plugin file"):
        installer.verify_archive(archive_path)


def test_installer_extracts_only_the_verified_archive_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = load_builder()
    installer = load_installer()
    archive_path = tmp_path / "substrate_wiki.zip"
    archive_path.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    original_verify = installer.verify_archive

    def verify_then_swap(path: Path, expected_sha256: str = "") -> dict[str, object]:
        result = original_verify(path, expected_sha256)
        path.write_bytes(b"locally replaced after verification")
        return result

    monkeypatch.setattr(installer, "verify_archive", verify_then_swap)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="changed after verification"):
        installer.install(
            archive_path,
            tmp_path / "hermes",
            expected_sha256=digest,
        )

    assert not (tmp_path / "hermes" / "plugins" / "substrate_wiki").exists()


def test_environment_path_rejects_a_symlink_before_resolving(tmp_path: Path) -> None:
    installer = load_installer()
    target = tmp_path / "profile.env"
    target.write_text("HERMES_API_URL=x\nHERMES_API_KEY=x\n", encoding="utf-8")
    link = tmp_path / "linked.env"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="non-symlink"):
        installer._resolve_env_path(link)


def test_fresh_install_rolls_back_plugin_when_service_install_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = load_builder()
    installer = load_installer()
    archive_path = tmp_path / "substrate_wiki.zip"
    archive_path.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    hermes_home = tmp_path / "hermes"

    def fail_service(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise OSError("service setup failed")

    monkeypatch.setattr(installer, "install_import_service", fail_service)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with pytest.raises(OSError, match="service setup failed"):
        installer.install(
            archive_path,
            hermes_home,
            expected_sha256=digest,
            install_service=True,
        )

    assert not (hermes_home / "plugins" / "substrate_wiki").exists()
    assert len(list((hermes_home / "plugins").glob("substrate_wiki.failed-*"))) == 1


def test_upgrade_restores_plugin_and_unit_when_service_reload_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = load_builder()
    installer = load_installer()
    archive_path = tmp_path / "substrate_wiki.zip"
    archive_path.write_bytes(builder.build_archive_bytes(source_commit="a" * 40))
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()

    fake_home = tmp_path / "user"
    hermes_home = fake_home / ".hermes"
    existing = hermes_home / "plugins" / "substrate_wiki"
    existing.mkdir(parents=True)
    (existing / "plugin.yaml").write_text(
        "name: substrate_wiki\nversion: 1.3.0\n", encoding="utf-8"
    )
    (existing / "v13-sentinel.txt").write_text("prior plugin", encoding="utf-8")

    env_path = fake_home / "profile.env"
    secret = "must-not-appear-in-unit-or-command"
    env_path.write_text(
        f"HERMES_API_URL=https://memory.example.invalid\nHERMES_API_KEY={secret}\n",
        encoding="utf-8",
    )
    units = fake_home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    unit_name = (
        "substrate-wiki-import-"
        + hashlib.sha256(os.fspath(hermes_home.resolve()).encode()).hexdigest()[:12]
        + "@.service"
    )
    unit_path = units / unit_name
    old_unit = "[Service]\nExecStart=/safe/v13/import-worker\n"
    unit_path.write_text(old_unit, encoding="utf-8")

    class PosixOSProxy:
        """Exercise the POSIX-only installer without changing pathlib's host OS."""

        name = "posix"

        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

    monkeypatch.setattr(installer, "os", PosixOSProxy())
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(installer, "_resolve_env_path", lambda explicit=None: env_path)
    systemctl_calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def fail_checked_reload(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        systemctl_calls.append((command, kwargs))
        if kwargs.get("check") is True:
            assert command == ("systemctl", "--user", "daemon-reload")
            assert "version: 2.0.3" in (existing / "plugin.yaml").read_text(
                encoding="utf-8"
            )
            plugin_rollbacks = list(
                (hermes_home / "plugins").glob("substrate_wiki.rollback-*")
            )
            assert len(plugin_rollbacks) == 1
            assert "version: 1.3.0" in (
                plugin_rollbacks[0] / "plugin.yaml"
            ).read_text(encoding="utf-8")
            assert "MemoryMax=256M" in unit_path.read_text(encoding="utf-8")
            unit_rollbacks = list(units.glob(f"{unit_name}.rollback-*"))
            assert len(unit_rollbacks) == 1
            assert unit_rollbacks[0].read_text(encoding="utf-8") == old_unit
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(installer.subprocess, "run", fail_checked_reload)

    with pytest.raises(subprocess.CalledProcessError):
        installer.install(
            archive_path,
            hermes_home,
            expected_sha256=digest,
            install_service=True,
            env_path=env_path,
        )

    assert "version: 1.3.0" in (existing / "plugin.yaml").read_text(encoding="utf-8")
    assert (existing / "v13-sentinel.txt").read_text(encoding="utf-8") == "prior plugin"
    failed_plugins = list((hermes_home / "plugins").glob("substrate_wiki.failed-*"))
    assert len(failed_plugins) == 1
    assert "version: 2.0.3" in (failed_plugins[0] / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    assert not list((hermes_home / "plugins").glob("substrate_wiki.rollback-*"))
    assert unit_path.read_text(encoding="utf-8") == old_unit
    assert not list(units.glob("*.rollback-*"))
    assert [call[0] for call in systemctl_calls] == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "daemon-reload"),
    ]
    observable_install_state = "\n".join(
        [
            unit_path.read_text(encoding="utf-8"),
            *(" ".join(command) for command, _kwargs in systemctl_calls),
            *(repr(kwargs) for _command, kwargs in systemctl_calls),
        ]
    )
    assert secret not in observable_install_state
    assert "hermes-gateway" not in observable_install_state


@pytest.mark.skipif(os.name != "posix", reason="systemd user services are POSIX-only")
def test_import_unit_failure_restores_previous_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    fake_home = tmp_path / "user"
    units = fake_home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    hermes_home = fake_home / ".hermes"
    plugin_target = hermes_home / "plugins" / "substrate_wiki"
    plugin_target.mkdir(parents=True)
    env_path = fake_home / ".env"
    env_path.write_text("HERMES_API_URL=x\nHERMES_API_KEY=x\n", encoding="utf-8")
    unit_name = (
        "substrate-wiki-import-"
        + hashlib.sha256(os.fspath(hermes_home.resolve()).encode()).hexdigest()[:12]
        + "@.service"
    )
    unit_path = units / unit_name
    unit_path.write_text("old-safe-unit\n", encoding="utf-8")

    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(installer, "_resolve_env_path", lambda explicit=None: env_path)

    def fail_reload(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if kwargs.get("check") is True:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(installer.subprocess, "run", fail_reload)
    with pytest.raises(subprocess.CalledProcessError):
        installer.install_import_service(hermes_home, plugin_target, env_path=env_path)

    assert unit_path.read_text(encoding="utf-8") == "old-safe-unit\n"
    assert not list(units.glob("*.rollback-*"))


@pytest.mark.skipif(os.name != "posix", reason="systemd user services are POSIX-only")
def test_import_unit_staging_failure_leaves_previous_unit_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer()
    fake_home = tmp_path / "user"
    units = fake_home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    hermes_home = fake_home / ".hermes"
    plugin_target = hermes_home / "plugins" / "substrate_wiki"
    plugin_target.mkdir(parents=True)
    env_path = fake_home / ".env"
    env_path.write_text("HERMES_API_URL=x\nHERMES_API_KEY=x\n", encoding="utf-8")
    unit_name = (
        "substrate-wiki-import-"
        + hashlib.sha256(os.fspath(hermes_home.resolve()).encode()).hexdigest()[:12]
        + "@.service"
    )
    unit_path = units / unit_name
    unit_path.write_text("old-safe-unit\n", encoding="utf-8")

    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(installer, "_resolve_env_path", lambda explicit=None: env_path)
    monkeypatch.setattr(
        installer.os,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("staging failed")),
    )
    systemctl_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda command, **kwargs: systemctl_calls.append(command),
    )

    with pytest.raises(PermissionError, match="staging failed"):
        installer.install_import_service(hermes_home, plugin_target, env_path=env_path)

    assert unit_path.read_text(encoding="utf-8") == "old-safe-unit\n"
    assert not list(units.glob("*.rollback-*"))
    assert systemctl_calls == []


def test_builder_excludes_generated_bytecode(tmp_path: Path) -> None:
    builder = load_builder()
    source = tmp_path / "substrate_wiki"
    source.mkdir()
    for filename in builder.REQUIRED_FILES:
        (source / filename).write_bytes((PLUGIN_SOURCE / filename).read_bytes())
    expected = builder.build_archive_bytes(source)
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "client.cpython-312.pyc").write_bytes(b"generated")

    assert builder.build_archive_bytes(source) == expected


def test_builder_rejects_unknown_release_files(tmp_path: Path) -> None:
    builder = load_builder()
    source = tmp_path / "substrate_wiki"
    source.mkdir()
    for filename in builder.REQUIRED_FILES:
        (source / filename).write_bytes((PLUGIN_SOURCE / filename).read_bytes())
    (source / "notes.txt").write_text("not a release file", encoding="utf-8")

    with pytest.raises(ValueError, match="release-clean.*unexpected"):
        builder.build_archive_bytes(source)


def test_builder_rejects_symlinked_source_members(tmp_path: Path) -> None:
    builder = load_builder()
    source = tmp_path / "substrate_wiki"
    source.mkdir()
    for filename in builder.REQUIRED_FILES:
        if filename != "README.md":
            (source / filename).write_bytes((PLUGIN_SOURCE / filename).read_bytes())
    try:
        (source / "README.md").symlink_to(PLUGIN_SOURCE / "README.md")
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="release-clean.*missing"):
        builder.build_archive_bytes(source)


def test_check_command_verifies_sha_archive_without_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    builder = load_builder()
    archive_path = tmp_path / "current" / "substrate_wiki.zip"
    releases_path = tmp_path / "releases"
    source_commit = "a" * 40

    builder.publish_release(
        builder.build_archive_bytes(source_commit=source_commit),
        archive_path=archive_path,
        releases_path=releases_path,
    )
    monkeypatch.setattr(builder, "ARCHIVE_PATH", archive_path)
    monkeypatch.setattr(builder, "RELEASES_PATH", releases_path)
    monkeypatch.setattr(builder, "_require_commit_contains_source", lambda commit: None)

    assert builder.main(["--check"]) == 0
    assert "release is current" in capsys.readouterr().out
