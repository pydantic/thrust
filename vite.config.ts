import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

export default defineConfig({
  server: {
    watch: {
      // ignore server directory (in particular the .venv) - which can make vite very hot
      ignored: [fileURLToPath(new URL("./server", import.meta.url))],
    },
  },
  build: {
    outDir: "dist",
    target: "es2022",
  },
});
