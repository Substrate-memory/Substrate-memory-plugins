# Releasing

Releases are immutable Git tags cut from protected `main` through the Release workflow.

1. Land the release contents on `main` through a reviewed pull request. Every commit
   must carry a DCO sign-off and pass all CI checks.
2. Dispatch the **Release** workflow with `candidate_sha` set to the exact reviewed
   `main` commit.
3. The workflow re-verifies the candidate (hygiene scan, tests, deterministic
   double-build of `substrate.zip`) and publishes tag `v<plugin.yaml version>` with
   `substrate.zip` and `SHA256SUMS` assets plus build attestation.
4. Never mutate a published tag or its assets. Fixes ship as a new reviewed commit
   and a new tag.
