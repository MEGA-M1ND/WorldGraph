import { defineConfig } from 'vite';
import { viteStaticCopy } from 'vite-plugin-static-copy';

/**
 * WorldGraph frontend build.
 *
 * Deliberately small. God's Eye View's 7,383-line vite.config.js hosted every external
 * API call and every credential inside dev middleware — a pattern that does not survive
 * `vite build` and puts key custody in the wrong process. WorldGraph's secrets live in the
 * FastAPI backend; this config's only jobs are Cesium's static assets and a dev proxy so
 * the browser talks to one origin.
 */

const CESIUM_BASE_URL = 'cesium';
const BACKEND = process.env.WORLDGRAPH_API_URL ?? 'http://127.0.0.1:8000';

export default defineConfig({
  base: './',
  define: {
    // Cesium resolves its workers, web assembly and asset bundles against this.
    CESIUM_BASE_URL: JSON.stringify(`./${CESIUM_BASE_URL}`),
  },
  plugins: [
    viteStaticCopy({
      targets: ['Workers', 'ThirdParty', 'Assets', 'Widgets'].map((dir) => ({
        src: `node_modules/cesium/Build/Cesium/${dir}`,
        dest: CESIUM_BASE_URL,
      })),
    }),
  ],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Same-origin `/api` in dev and prod alike, so no CORS preflight and no
      // environment-specific base URL in the client bundle.
      '/api': { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    target: 'es2022',
    sourcemap: true,
    chunkSizeWarningLimit: 4096, // Cesium is large; the warning is noise, not a signal.
  },
});
