// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
// SPDX-FileContributor: Lorenzo Massaro
// SPDX-License-Identifier: AGPL-3.0-only

type DetwinRuntimeConfig = {
  apiBaseUrl?: string;
  platformBaseUrl?: string;
  releaseTrack?: string;
  runtimeMode?: string;
};

declare global {
  interface Window {
    __DETWIN_RUNTIME_CONFIG__?: DetwinRuntimeConfig;
  }
}

function runtimeConfig(): DetwinRuntimeConfig {
  if (typeof window === "undefined") return {};
  return window.__DETWIN_RUNTIME_CONFIG__ || {};
}

export function runtimeConfigValue(key: keyof DetwinRuntimeConfig) {
  const value = runtimeConfig()[key];
  return typeof value === "string" ? value.trim() : "";
}
