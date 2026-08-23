// SPDX-FileCopyrightText: 2026 Eternal Tech SRL <info@eternaltech.ai>
// SPDX-FileContributor: Lorenzo Massaro
// SPDX-License-Identifier: AGPL-3.0-only

import React from "react";
import ReactDOM from "react-dom/client";

import { CockpitApp } from "./App";
import "./new-ui/neural-cockpit.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <CockpitApp />
  </React.StrictMode>,
);
