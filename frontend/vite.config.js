import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import wasm from 'vite-plugin-wasm'
import topLevelAwait from 'vite-plugin-top-level-await'

export default defineConfig({
  plugins: [react(), wasm(), topLevelAwait()],
  server: {
    port: 5180,
    proxy: {
      '/api': 'http://localhost:8787',
      // CRDT room sync (y-websocket) and the terminal PTY proxied to the backend.
      '/ws': { target: 'ws://localhost:8787', ws: true },
      '/pty': { target: 'ws://localhost:8787', ws: true },
    },
  },
  // typst.ts ships large wasm; don't let esbuild choke on it during dep optimization.
  optimizeDeps: { exclude: ['@myriaddreamin/typst-ts-renderer', '@myriaddreamin/typst-ts-web-compiler'] },
})
