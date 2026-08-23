<!--
SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
SPDX-FileContributor: Lorenzo Massaro
SPDX-License-Identifier: AGPL-3.0-only
-->

# Brain Core

Brain Core is the public, AGPL-safe visualization of a selected AGVM brain. Its
interactive points represent real graph records only. The renderer must not add
decorative memory nodes, animation-only nodes or a second synthetic anatomy
that could be mistaken for stored data.

## Data Contract

- A rendered node maps to one node identifier returned by the local Core API.
- Empty brains show an explicit create, import or Grow action.
- Search animations may highlight only nodes already present in the rendered
  graph snapshot.
- Display density is a view choice and never changes stored memory.
- Selecting a point may expose content, source, links, confidence and revision
  metadata already authorized by the API.

The supported density labels are Focus, Balanced, Detailed and Full. The
display projection may use the active Brain Profile V1, but search coordinates
remain independent and authoritative.

## Delta States

Preview surfaces may distinguish real proposed changes:

- mint: new node;
- violet: moved or reweighted node;
- red: rejected or removed node;
- ink or neutral: unchanged node;
- dashed treatment: profile scoring is pending.

No preview color implies that an apply already occurred. Grow, bootstrap,
geometry and profile changes remain review-first and require their explicit
apply contracts.

## Public And Private UI Boundary

The public export uses the independent AGPL implementation under
`public-core-docs/ui-src`. The private rich Cloud cockpit under
`agvm_cockpit_prototype/src/new-ui` is not copied by the public allowlist and is
explicitly covered by the private denylist. Similar product behavior does not
change the source-license boundary.
