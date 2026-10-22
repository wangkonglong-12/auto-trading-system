import { fileURLToPath, URL } from "node:url";

import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "../../src/auto_trading_system/apps/local_console/static",
    emptyOutDir: true,
    sourcemap: false,
    manifest: false,
    target: "es2022",
    rollupOptions: {
      input: {
        legacy: fileURLToPath(new URL("./index.html", import.meta.url)),
        node: fileURLToPath(new URL("./node.html", import.meta.url)),
      },
      output: {
        manualChunks(id) {
          if (id.includes("/node_modules/echarts/") || id.includes("/node_modules/zrender/")) {
            return "vendor-charts";
          }
          if (
            id.includes("/node_modules/naive-ui/") ||
            id.includes("/node_modules/vueuc/") ||
            id.includes("/node_modules/css-render/")
          ) {
            return "vendor-ui";
          }
          if (id.includes("/node_modules/vue/") || id.includes("/node_modules/vue-router/")) {
            return "vendor-vue";
          }
          return undefined;
        },
      },
    },
  },
  test: {
    environment: "happy-dom",
    setupFiles: ["./src/test/setup.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary"],
      include: ["src/**/*.{ts,vue}"],
      exclude: ["src/main.ts", "src/nodeMain.ts", "src/App.vue", "src/**/*.d.ts", "src/test/**"],
      thresholds: {
        lines: 80,
        functions: 80,
        statements: 80,
        branches: 75,
      },
    },
  },
});
