# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build

ARG VITE_AGVM_UI_SHELL_PROFILE=public_core
ENV VITE_AGVM_UI_SHELL_PROFILE=${VITE_AGVM_UI_SHELL_PROFILE}

WORKDIR /app/agvm_cockpit_prototype
COPY packages/detwin-design-tokens /app/packages/detwin-design-tokens
COPY agvm_cockpit_prototype/package.json agvm_cockpit_prototype/package-lock.json ./
COPY agvm_cockpit_prototype/tsconfig.json ./
COPY agvm_cockpit_prototype/tsconfig.app.json ./
COPY agvm_cockpit_prototype/tsconfig.node.json ./
COPY agvm_cockpit_prototype/vite.config.ts ./
RUN npm ci

COPY sdk/typescript /app/sdk/typescript
COPY agvm_cockpit_prototype/index.html ./
COPY agvm_cockpit_prototype/public ./public
COPY agvm_cockpit_prototype/src ./src
COPY agvm_cockpit_prototype/scripts/write-release-artifact-manifest.mjs /tmp/write-release-artifact-manifest.mjs

# These values change for every release but are only needed by the artifact
# manifest step. Declaring them here keeps npm ci and source-copy layers
# reusable across Core-only release commits.
ARG AGVM_IMAGE_VERSION=0.0.0-dev
ARG AGVM_IMAGE_REVISION=unknown
ARG AGVM_IMAGE_CREATED=1970-01-01T00:00:00Z
ARG AGVM_RELEASE_SOURCE_SHA=
ARG AGVM_RELEASE_BUNDLE_ID=
RUN npm run build \
    && node /tmp/write-release-artifact-manifest.mjs \
        --root /app/agvm_cockpit_prototype/dist \
        --output /app/agvm_cockpit_prototype/dist/.well-known/detwin-artifact-manifest.json \
        --version-output /app/agvm_cockpit_prototype/dist/version.json \
        --component core_ui \
        --version "${AGVM_IMAGE_VERSION}" \
        --revision "${AGVM_IMAGE_REVISION}" \
        --source-sha "${AGVM_RELEASE_SOURCE_SHA}" \
        --release-bundle "${AGVM_RELEASE_BUNDLE_ID}" \
        --created "${AGVM_IMAGE_CREATED}"

FROM nginxinc/nginx-unprivileged:1.28-alpine@sha256:7377697a821c131a924a7105fafbe7414db4e9fcc77a6f08f776f33f141ec3f8 AS runtime

ARG AGVM_IMAGE_VERSION=0.0.0-dev
ARG AGVM_IMAGE_REVISION=unknown
ARG AGVM_IMAGE_CREATED=1970-01-01T00:00:00Z
ARG AGVM_RELEASE_SOURCE_SHA=
ARG AGVM_RELEASE_BUNDLE_ID=

LABEL org.opencontainers.image.title="Detwin Core UI" \
      org.opencontainers.image.version="${AGVM_IMAGE_VERSION}" \
      org.opencontainers.image.revision="${AGVM_IMAGE_REVISION}" \
      org.opencontainers.image.created="${AGVM_IMAGE_CREATED}" \
      org.opencontainers.image.vendor="Eternal Tech SRL" \
      ai.detwin.release.source_sha="${AGVM_RELEASE_SOURCE_SHA}" \
      ai.detwin.release.bundle="${AGVM_RELEASE_BUNDLE_ID}" \
      ai.detwin.artifact.manifest="/usr/share/nginx/html/.well-known/detwin-artifact-manifest.json"

COPY agvm_cockpit_prototype/nginx.core.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/agvm_cockpit_prototype/dist /usr/share/nginx/html
USER root
COPY agvm_cockpit_prototype/docker-entrypoint.d/10-detwin-runtime-config.sh /docker-entrypoint.d/10-detwin-runtime-config.sh
RUN sed -i 's/\r$//' /docker-entrypoint.d/10-detwin-runtime-config.sh \
    && chmod 755 /docker-entrypoint.d/10-detwin-runtime-config.sh \
    && touch /usr/share/nginx/html/detwin-runtime-config.js \
    && chown 101:0 /usr/share/nginx/html/detwin-runtime-config.js
USER 101

EXPOSE 3020

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1:3020/health || exit 1
