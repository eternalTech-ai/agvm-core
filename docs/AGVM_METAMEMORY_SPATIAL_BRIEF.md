<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# AGVM Public Metamemory Spatial Brief

This public brief is the immutable runtime policy source for coordinate-first
Search in AGVM Core. Runtime state supplies the per-brain atlas, topology,
calibration, and revision chain; this document supplies only public policy.

## COORDINATE SYSTEM

Use normalized semantic `x`, `y`, and `z` coordinates. A coordinate or region
is a navigation target, not a claim of truth. Coordinates must be validated
against the active brain revision before traversal.

## LANDING POLICY

Start from inverse answer strands. The AI planner must author at least one
bounded destination for every admitted strand. A destination uses `region_ref`,
an explicit coordinate, or `novel_region_candidate`. Do not route by
`memory_type`, `guide_area`, labels, tags, or other display metadata.

## PATH POLICY

Execute destinations as landing spheres and inter-destination tubes. Walk
local links for nearby continuity, use highways only for justified long-range
movement, and treat evidence edges as provenance. Preserve route receipts and
the active revision chain.

## TOPOLOGY

Topology is the navigable structure of the active brain. Density lobes,
bridges, gateways, attraction priors, and repulsion priors may guide motion
only when they are bound to the current atlas and matrix revisions.

## SOURCE EVIDENCE

Candidate nodes are not answer evidence until promoted by the evidence stage.
Hydrate exact source anchors only for promoted evidence. Preserve document,
span, chronology, and provenance identifiers in the delivered context.

## FAILURE POLICY

Search fails closed when the planner or required AI material is unavailable.
Provider quota exhaustion remains a provider blocker. No deterministic or
metadata-based landing fallback may make a blocked run appear usable or
terminal for the client.
