import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
export default defineConfig({ plugins: [vue()], build: { outDir: 'dist', emptyOutDir: true }, server: { proxy: { '/v1': 'http://127.0.0.1:18765', '/media': 'http://127.0.0.1:18765' } } })
