# Releasing

1. Choose a new semantic version. Never reuse an existing tag or immutable asset path.
2. Update `src/substrate_wiki/plugin.yaml`, installer `EXPECTED_VERSION`, client version, README, compatibility matrix, and changelog.
3. Run all tests, compile checks, publication scanner, and bounded importer benchmark.
4. Commit the release-clean source.
5. Build with exact provenance:

   ```bash
   HERMES_PLUGIN_SOURCE_COMMIT="$(git rev-parse HEAD)" python scripts/build_plugin.py
   python scripts/build_plugin.py --check
   ```

6. Verify archive and installer SHA-256 values independently.
7. Run an adversarial review against the exact commit and artifacts.
8. Tag the reviewed commit and publish the exact generated archive and installer once.
9. Read the GitHub release assets back; verify byte hashes and provenance.
10. Update downstream pinned-release references in Substrate-v2 through a separate PR.

A release must not contain credentials, private history, private server endpoints, production evidence, or server implementation. The runtime source allowlist is closed by the builder.

Version 1.4.1 is an imported immutable release. Its provenance points to its original Substrate-v2 commit; do not rebuild it from this repository.
