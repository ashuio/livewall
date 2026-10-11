import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The backend serves the built app at / and the API at /api/*, so production is
// same-origin with no proxy. In dev the two are separate processes, so proxy
// /api to the backend — the app code stays origin-agnostic either way.
const BACKEND = process.env.LIVEWALL_BACKEND ?? 'http://127.0.0.1:8470';

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5183,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/health': { target: BACKEND, changeOrigin: true },
    },
  },
});
