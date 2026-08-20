# Modules

AGVM Core is open source under AGPL-3.0. Advanced modules are not bundled into
the public core repository.

The first paid module group is planned as cloud-only:

- Clone App;
- Grow Studio;
- Maintain Studio;
- advanced chat/product surfaces.

For this release, those advanced workflows run in Detwin Cloud behind account
and workspace entitlement. Local AGVM Core still exposes the raw MCP primitives:
Grow/source preview, write preview/commit, Sleep, Evolve, Matrix calibration,
memory OS lists and route inspection.

## Boundary

The public core repository contains:

- local API and UI packaging;
- local stdio MCP server;
- raw core MCP tool contracts and endpoints;
- TypeScript contracts required by the core UI;
- AGPL license, notices and local setup docs.

The public core repository does not contain:

- private cloud platform code;
- billing implementation or Stripe secrets;
- paid module source code;
- private runtime images;
- local paid module download helpers.

Local paid module delivery can be added later only after the helper,
verification, revocation and private distribution path is fully tested.
