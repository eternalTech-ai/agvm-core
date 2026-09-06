#!/bin/sh
# SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
# SPDX-FileContributor: Lorenzo Massaro
# SPDX-License-Identifier: AGPL-3.0-only

set -eu

target="/usr/share/nginx/html/detwin-runtime-config.js"

clean_url() {
  value="${1:-}"
  if [ -z "$value" ]; then
    printf '%s' ""
    return 0
  fi
  if printf '%s' "$value" | grep -Eq '^https?://' && ! printf '%s' "$value" | grep -Eq '[[:space:]"\\]'; then
    printf '%s' "$value" | sed 's:/*$::'
    return 0
  fi
  echo "Invalid Detwin runtime URL" >&2
  return 1
}

clean_token() {
  value="${1:-}"
  if [ -z "$value" ]; then
    printf '%s' ""
    return 0
  fi
  if printf '%s' "$value" | grep -Eq '^[A-Za-z0-9_.-]+$'; then
    printf '%s' "$value"
    return 0
  fi
  echo "Invalid Detwin runtime token" >&2
  return 1
}

api_url="$(clean_url "${AGVM_CORE_UI_API_URL:-${VITE_API_URL:-}}")"
platform_url="$(clean_url "${AGVM_CORE_UI_PLATFORM_URL:-${VITE_PLATFORM_URL:-}}")"
release_track="$(clean_token "${AGVM_CORE_UI_RELEASE_TRACK:-}")"
runtime_mode="$(clean_token "${AGVM_CORE_UI_RUNTIME_MODE:-${VITE_AGVM_RUNTIME_MODE:-}}")"

cat > "$target" <<EOF
window.__DETWIN_RUNTIME_CONFIG__ = Object.freeze({
  apiBaseUrl: "$api_url",
  platformBaseUrl: "$platform_url",
  releaseTrack: "$release_track",
  runtimeMode: "$runtime_mode"
});
EOF
