import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The browser talks only to this dev/preview server. Requests to /api are
// forwarded to the Python API, so the API needs no CORS headers and its
// address never has to be exposed to the page.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const target = env.API_PROXY_TARGET || "http://127.0.0.1:8765";
  const proxy = {
    "/api": {
      target,
      changeOrigin: true,
      rewrite: (path: string) => path.replace(/^\/api/, ""),
    },
  };
  return {
    plugins: [react()],
    build: {
      rollupOptions: {
        output: { manualChunks: { vendor: ["react", "react-dom", "recharts"] } },
      },
      chunkSizeWarningLimit: 650,
    },
    server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
    preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy },
  };
});
