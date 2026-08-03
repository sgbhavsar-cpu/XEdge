import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Separate from vite.config.ts (not merged via `mergeConfig`): the app's
// own build/dev config (outDir, proxy) has nothing to do with how tests
// run, and keeping them apart avoids test-only settings leaking into
// what `npm run build` actually ships.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
    // e2e/ is Playwright's (playwright.config.ts), run via `npm run
    // test:e2e` -- Vitest's default include pattern would otherwise
    // also try to run those files itself and fail (different test API).
    exclude: ['node_modules', 'e2e'],
  },
})
