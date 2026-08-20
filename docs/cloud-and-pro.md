# Cloud And Pro

AGVM Core is local-first and does not require an AGVM cloud account. Detwin
Cloud is a separate product surface for users who want managed memory, Hosted
MCP, usage metering and advanced cloud modules.

## Local Free Core

Local free core runs on the user's machine:

- local Docker API and UI;
- local brain registry and volumes;
- local stdio MCP bridge;
- local provider key configuration;
- no AGVM cloud login required.

## Hosted AGVM

Hosted AGVM is intended to provide:

- hosted MCP access without running the local product;
- managed tenant/workspace storage;
- account login and billing;
- usage/token metering;
- Pro cloud module entitlements;
- optional cloud persistence for users who explicitly choose hosted mode.

Hosted mode must be tenant-scoped and server-authorized. A hosted token must not
grant access to another user's brain, module state or usage records.

## AGVM Pro

The planned first paid package is a single AGVM Pro bundle. For this release it
unlocks advanced modules in Cloud AGVM:

- Clone App;
- Grow Studio;
- Maintain Studio;
- advanced chat/product surfaces.

Users can create or sync a brain to Detwin Cloud when they want these advanced
workflows. Local AGVM Core remains usable without login for local memory and raw
MCP. Local paid module downloads are not part of this release.

## What Is Public Here

This repository may include:

- raw core MCP contracts and endpoints;
- local core placeholders for cloud-only module entry points;
- documentation explaining how activation works at a high level;
- SDK-facing interfaces that do not expose private platform implementation.

This repository must not include:

- Stripe secret keys or billing implementation;
- private platform API code;
- private AWS infrastructure;
- paid module source code;
- customer data, tenant data or local brain exports.

## Current Status

Cloud and Pro are product targets, not implied by a local AGVM Core install. If
a public core screen references hosted or Pro capabilities, it must make clear
whether the feature requires Detwin Cloud or is arriving later.
