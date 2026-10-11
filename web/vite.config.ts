import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: { outDir: '../cthulhu/static', emptyOutDir: true },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:7500', ws: true } } },
})
