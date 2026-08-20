import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  preview: {
    allowedHosts: [
      "cloud.detwin.ai",
      "detwin.ai",
      "app.detwin.ai",
      "localhost",
      "127.0.0.1",
    ],
  },
});
