# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build

# Build with the repository root as Docker context so the UI can consume the
# shared TypeScript SDK without vendoring contract copies into the cockpit app.
ARG VITE_API_URL=http://localhost:8010
ARG VITE_AGVM_UI_SHELL_PROFILE=public_core
ENV VITE_API_URL=${VITE_API_URL} \
    VITE_AGVM_UI_SHELL_PROFILE=${VITE_AGVM_UI_SHELL_PROFILE}

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
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28-alpine@sha256:7377697a821c131a924a7105fafbe7414db4e9fcc77a6f08f776f33f141ec3f8 AS runtime

COPY agvm_cockpit_prototype/nginx.core.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/agvm_cockpit_prototype/dist /usr/share/nginx/html

EXPOSE 3020

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=6 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1:3020/health || exit 1
