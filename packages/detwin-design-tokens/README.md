<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Detwin Design Tokens

Canonical v3 visual tokens generated from `src/tokens.json`. The source binds
the signed redesign package aggregate and emits identical CSS and TypeScript
surfaces. Product code consumes generated exports and does not copy raw colors,
radii, typography, control sizes, or layout values.

`visualProjection` is a separately versioned, display-only contract for the 3D
Brain renderer. It retains the accepted v3 flat reference colors and the local
teal and cloud violet depth gradients. Those ramps may project runtime, depth,
density, selection, candidate, revision, or diff state; they must never encode
semantic E/G values, backend truth, risk, success, or shell decoration. The
generated manifest records that boundary as
`detwin.visual_color_projection.v1`.
