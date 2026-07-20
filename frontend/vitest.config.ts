import path from 'node:path'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Test-only config. Kept separate from vite.config.ts so the app build's typecheck
// (tsc -b, which includes vite.config.ts) is not exposed to vitest's vendored Vite
// types. Vitest auto-loads this file. Tailwind is intentionally omitted: component
// styling is irrelevant to behavior tests.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
