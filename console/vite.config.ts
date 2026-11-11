import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build is served by BeeBox itself at /console, so every emitted URL must
// carry that prefix. `npm run dev` proxies /v1 to a local BeeBox so the same
// client code works against port 5173 during development.
export default defineConfig({
  base: "/console/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": "http://127.0.0.1:17494",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
