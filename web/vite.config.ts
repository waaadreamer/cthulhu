import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// A checkout whose cthulhu/static is served live carries this untracked marker. Building
// there would publish assets before managed deploy, so builds must run in another clone.
const served = fileURLToPath(new URL('../.served-checkout', import.meta.url))

export default defineConfig(({ command }) => {
  if (command === 'build' && existsSync(served))
    throw new Error(`refusing to build in a live served checkout (${served}); build in a separate clone and use managed deploy`)
  return {
    plugins: [react()],
    base: './',
    build: { outDir: '../cthulhu/static', emptyOutDir: true },
    server: { proxy: { '/api': { target: 'http://127.0.0.1:7500', ws: true } } },
  }
})
