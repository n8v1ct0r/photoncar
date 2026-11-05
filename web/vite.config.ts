import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

// `vite build` makes the device app (web/dist, history routes, the device API).
// `vite build --mode embed` makes the portfolio embed (web/dist-embed): relative asset paths,
// hash routes, and the static data files that `photoncar export-embed` writes next to it.
export default defineConfig(({ mode }) => {
  const embed = mode === "embed";
  return {
    plugins: [preact()],
    base: embed ? "./" : "/",
    server: { proxy: { "/api": "http://127.0.0.1:8000" } },
    build: { outDir: embed ? "dist-embed" : "dist", sourcemap: false, emptyOutDir: true },
  };
});
