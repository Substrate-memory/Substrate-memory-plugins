# Open-source and commercial boundary

This boundary is declared on day one and will not be moved later. Substrate asks users to entrust working-life OAuth grants to the product; auditability is therefore a functional requirement. The code is intentionally forkable. The held value is operated credential custody, accumulated entity/policy/decision context, and assumed liability—not a surprise restriction on previously open code.

## Permanent boundary

| Open — permissive MIT | Held |
|---|---|
| Hermes plugin, client, local runtime | Hosted multi-tenant broker |
| Memory extraction, entity model, credential containment, privacy deletion | Cross-organizational graph |
| Policy compiler (natural language → formal policy) | Audit/attestation + insurance-backed decisions |
| Single-user, local, self-hosted, free forever | The action meter |

## Current implementation status

Release `v1.5.0` contains the Hermes `substrate_wiki` plugin/client, local spool and checkpoint machinery, client-side credential redaction, history replay, deterministic packaging, and installation tooling. It requires a configured Substrate server.

The local runtime, local entity model, privacy-deletion implementation, and policy compiler are on the open side of the permanent boundary but are **not implemented in this release**. `v1.5.0` does not authorize agent actions and does not provide a no-server mode. Those absences are explicit product gaps, not held commercial features.

## Paid hosted tier

The paid hosted tier covers hosted brokerage, multi-user operation, cross-organizational graph services, audit/attestation, and insurance-backed decisions. The commercial meter is per authorized action, never per seat. None of those held services is licensed or distributed by this repository.

## What we will never do

- Add an action meter or seat meter to the open components after adoption.
- Charge for the single-user local self-hosted path.
- Move code from the open column into the held column after release.
- Describe an unverified build as certified or use a certification mark before its ownership and criteria are legally established.

## Marks

The MIT license grants copyright permissions, not trademark rights. No certification mark is claimed by this repository. The product-name and certification-mark decisions require separate legal review; the name “Substrate” is not treated here as an approved or registered mark.
