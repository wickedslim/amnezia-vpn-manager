import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'tests/browser',testMatch:'*.spec.js',fullyParallel:false,
  use:{baseURL:'http://127.0.0.1:8774',viewport:{width:393,height:852}},
  webServer:{command:'python scripts/miniapp_preview.py --port 8774',url:'http://127.0.0.1:8774',reuseExistingServer:false},
  reporter:'list'
});
