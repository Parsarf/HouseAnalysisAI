import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { rmSync } from "node:fs";
import { resolve } from "node:path";

const devOnlyMsw = {
  name: "remove-msw-from-production-bundle",
  closeBundle() {
    rmSync(resolve("dist/mockServiceWorker.js"), { force: true });
  },
};

export default defineConfig({ plugins: [react(), devOnlyMsw] });
