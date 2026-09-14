/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  test: {
    // jsdom, not node: both hooks under test are browser-coupled - one drives an EventSource,
    // the other schedules window.setTimeout against the pawn animation.
    environment: 'jsdom',
    globals: true,
    // Only the hooks are covered for now. The Phaser scene is deliberately out of scope: it
    // renders to a canvas Phaser owns imperatively, and testing it meaningfully needs a
    // different tool than a jsdom unit test.
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
