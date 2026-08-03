import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// ADR-013 §6: served by the Fleet Manager itself, consuming its REST API
// -- built straight into xedge/fleet/static/dashboard/ (already inside the
// xedge/ package tree) so a non-editable `pip install .` picks it up via
// hatchling's default package-data inclusion, with no pyproject.toml
// force-include entry needed (unlike config/schema/, which lives outside
// xedge/ and does need one). Gitignored like any other build artifact --
// the Dockerfile's frontend-builder stage runs `npm run build` before the
// Python build stage's `pip install`, so the files exist on disk at
// packaging time either way.
//
// The dev server proxies API calls to the Fleet Manager's admin port
// (8090 by default, `xedge-fleet-manager --port`) so `npm run dev` talks
// to a real, separately-running manager process without a CORS dance.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../xedge/fleet/static/dashboard',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // `secure: false`: the manager's dev-mode TLS cert is self-signed
      // (xedge-fleet-manager auto-generates one) -- the proxy is a plain
      // dev convenience, not a security boundary the browser ever sees.
      '/api/v1/fleet': { target: 'https://localhost:8090', secure: false },
    },
  },
})
