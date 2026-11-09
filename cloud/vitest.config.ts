import { cloudflareTest } from '@cloudflare/vitest-plugin';
import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: { include: ['test/**/*.test.ts'] },
  plugins: [cloudflareTest({
    remoteBindings: false,
    wrangler: { configPath: './wrangler.jsonc' },
    miniflare: { bindings: {
      DEVICE_AUTH_URL: 'https://identity.example.com', TRANSCRIBE_ENGINE: 'external',
    } },
  })],
});
