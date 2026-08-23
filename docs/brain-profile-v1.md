<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Brain Profile V1

Brain Profile V1 personalizes reranking without replacing the existing AGVM
search pipeline. It is deliberately bounded to the current V1 semantic space.
It does not import Spatial V2, a control plane, or arbitrary AI-authored
dimensions.

## Fixed Semantic Space

The profile operates on exactly the 12 canonical routing dimensions already
defined by AGVM Core. A valid signed profile contains:

- 12 positive diagonal weights;
- bounded low-rank factors with rank no greater than 4;
- a reranking blend no greater than 35 percent;
- a `3 x 12` display projection;
- revision, benchmark and integrity metadata.

The server validates the bounded parameters and constructs the positive
semidefinite profile transform. An AI client never writes a raw matrix, node
coordinates or bulk node updates directly.

## Search Safety

Candidate generation, lexical retrieval, document lanes, path expansion,
trust, evidence, temporal policy and answer delivery remain outside profile
ownership. A missing, disabled, shadow or invalid profile must fall back to the
normal V1 ranking.

Profile preview is shadow-only. Activation is permitted only after an
authoritative benchmark demonstrates all of the following:

- weighted quality improvement of at least 5 percent;
- no critical metric regression greater than 2 percent;
- p95 latency no greater than 1.20 times the baseline.

Release operators must keep activation disabled unless the benchmark producer,
authority checks and retrieval regression suite pass. The public contract is
not evidence that a particular deployment has passed those gates.

## MCP Contract

The public registry exposes:

- `brain_profile_preview`;
- `brain_profile_apply`;
- `brain_profile_rollback`.

The public package keeps these contracts discoverable but does not ship the
profile benchmark, reranking runtime, activation store or rollback executor.
Preview, fitting, apply and rollback are Cloud-backed in the public build and
return a structured `action_contract`. A local module lease cannot unlock an
implementation that is not present, and the fallback retrieval path preserves
the authoritative baseline order.

## Brain Core Projection

The `3 x 12` projection is for Brain Core visualization only. It must not
overwrite the operational coordinates used by retrieval. See
[Brain Core](brain-core.md) for the public visualization boundary.
