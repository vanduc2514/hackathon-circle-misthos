import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The API is proxied so the browser sees a single origin. That keeps cookies and
// CORS out of the picture entirely.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Bind every interface. Without this Vite listens on ::1 only, so
    // 127.0.0.1:5173 refuses the connection while localhost:5173 works.
    // It also makes the demo reachable from a phone on the same network.
    host: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // So the API reference is reachable from the app's own origin.
      '/docs': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/openapi.json': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
