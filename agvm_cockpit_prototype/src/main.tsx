import React from "react";
import ReactDOM from "react-dom/client";

import { CockpitApp } from "./App";
import "./new-ui/neural-cockpit.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <CockpitApp />
  </React.StrictMode>,
);
