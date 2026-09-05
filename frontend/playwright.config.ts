import { defineConfig, devices } from '@playwright/test';

/**
 * E2E configuration.
 *
 * Assumes the backend is already running on :8000 (`uvicorn app.main:app`) and starts the
 * Vite dev server itself. Serial, single-worker: the tests share one backend whose
 * simulation scenarios are process state, and parallel runs would interleave them.
 */
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
    // WebGL needs a real surface; 1600x950 is a realistic laptop viewport and the
    // layout's three-column breakpoint is above it.
    viewport: { width: 1600, height: 950 },
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // After the spread: the device preset carries its own 1280x720 viewport, and a
        // project-level value set before it would be overwritten.
        viewport: { width: 1600, height: 950 },
        // Headless Chromium falls back to SwiftShader for WebGL, and Cesium refuses to
        // start without a GL context. These flags are what make the globe render in CI.
        launchOptions: {
          executablePath: process.env.WORLDGRAPH_CHROMIUM || undefined,
          args: [
            '--use-gl=swiftshader',
            '--enable-unsafe-swiftshader',
            '--disable-gpu-sandbox',
            '--no-sandbox',
          ],
        },
      },
    },
  ],
  webServer: {
    // Locally this is the dev server (instant HMR while iterating). CI sets
    // WORLDGRAPH_E2E_COMMAND to serve the production build instead, which is both the
    // artifact users receive and fast enough to start inside the timeout — a cold Vite
    // dev start transforming ~1500 Cesium modules is not.
    command: process.env.WORLDGRAPH_E2E_COMMAND ?? 'npm run dev',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: !process.env.CI,
    // Generous even for preview: a cold runner still has to boot Node and read the bundle.
    timeout: 180_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
