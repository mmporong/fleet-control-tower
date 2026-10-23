import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({
  base: '/3d/',
  plugins: [react()],
  build: { outDir: '../web/3d', emptyOutDir: true, sourcemap: false },
  server: { host: '127.0.0.1', proxy: { '/api': 'http://127.0.0.1:8097' } },
});
