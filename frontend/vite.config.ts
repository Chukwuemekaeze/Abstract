import path from 'node:path'
import { sentryVitePlugin } from '@sentry/vite-plugin'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Upload source maps to Sentry at build time so production stack traces map back
// to our source. Gated on SENTRY_AUTH_TOKEN (a build-time secret, not a VITE_ var,
// so it never reaches the client); without it the plugin is skipped and local
// builds are unaffected.
const sentryToken = process.env.SENTRY_AUTH_TOKEN

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    ...(sentryToken
      ? [
          sentryVitePlugin({
            org: process.env.SENTRY_ORG,
            project: process.env.SENTRY_PROJECT,
            authToken: sentryToken,
          }),
        ]
      : []),
  ],
  // Emit source maps so the Sentry plugin can upload them (and unminify traces).
  build: { sourcemap: true },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      // Proxy API calls to the FastAPI backend so the browser sees a single
      // origin during dev (no CORS, cookies work for future real auth).
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
