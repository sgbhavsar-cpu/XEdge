import { defineConfig } from '@playwright/test'

// Starts the Vite dev server automatically; does *not* start the Fleet
// Manager backend these e2e tests exercise for real (no mocked fetch,
// per this sprint's testing decision) -- that needs a real Postgres
// instance and isn't something Playwright itself should own. Run
// `xedge-fleet-manager` against a real database first (see README), same
// setup `npm run dev`'s proxy already assumes.
export default defineConfig({
  testDir: './e2e',
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
  },
  use: {
    baseURL: 'http://localhost:5173',
  },
})
