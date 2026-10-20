import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import type { IncomingMessage } from 'node:http';
import type { Duplex } from 'node:stream';
import { proxyUpgrade } from 'httpxy';
import { defineNuxtConfig } from 'nuxt/config';

const faviconHash = createHash('sha256')
  .update(readFileSync(new URL('./public/favicon.ico', import.meta.url)))
  .digest('hex')
  .slice(0, 12);

const appleIconHash = createHash('sha256')
  .update(readFileSync(new URL('./public/images/favicon.png', import.meta.url)))
  .digest('hex')
  .slice(0, 12);

const isProd = 'production' === process.env.NODE_ENV;
// devProxy exists only when NUXT_API_URL is set. Include the full backend
// path (e.g. http://127.0.0.1:8000/api): the /api/ mount prefix is not
// forwarded to the target. Nitro's upgrade handler bypasses devProxy, so the
// ready hook forwards /api/events upgrades directly to the backend origin.
let extraNitro: Record<string, unknown> = {};
const API_URL = process.env.NUXT_API_URL;
if (API_URL) {
  extraNitro = {
    devProxy: {
      '/api/': {
        target: API_URL,
        changeOrigin: true,
      },
    },
  };
}

export default defineNuxtConfig({
  ssr: false,
  hooks: {
    ready(nuxt) {
      if (!nuxt.options.dev || !API_URL) return;
      const upgrade = nuxt.server.upgrade;
      nuxt.server.upgrade = (req: IncomingMessage, socket: Duplex, head: Buffer) => {
        if (req.url?.split('?', 1)[0] === '/api/events') {
          return proxyUpgrade(new URL(API_URL).origin, req, socket, head, {
            changeOrigin: true,
          }).catch(() => socket.destroy());
        }
        return upgrade(req, socket, head);
      };
    },
  },
  runtimeConfig: {
    public: {
      wss: process.env.NUXT_PUBLIC_WSS ?? '',
    },
  },
  sourcemap: false === isProd,
  devtools: { enabled: true },
  devServer: {
    port: 8082,
    host: '0.0.0.0',
  },
  colorMode: {
    preference: 'dark',
    fallback: 'dark',
    classSuffix: '',
  },
  css: ['~/assets/css/tailwind.css'],
  app: {
    buildAssetsDir: 'assets',
    head: {
      meta: [
        { charset: 'utf-8' },
        { name: 'viewport', content: 'width=device-width, initial-scale=1.0, maximum-scale=1.0' },
        { name: 'theme-color', content: '#020817' },
        { name: 'mobile-web-app-capable', content: 'yes' },
        { name: 'apple-mobile-web-app-capable', content: 'yes' },
        { name: 'apple-mobile-web-app-status-bar-style', content: 'black-translucent' },
        { name: 'apple-mobile-web-app-title', content: 'Millwright' },
      ],
      base: { href: '/' },
      link: [
        { rel: 'icon', type: 'image/x-icon', href: `favicon.ico?v=${faviconHash}` },
        { rel: 'manifest', href: 'manifest.webmanifest?v=100' },
        {
          rel: 'apple-touch-icon',
          sizes: '1024x1024',
          href: `images/favicon.png?v=${appleIconHash}`,
        },
        { rel: 'apple-touch-startup-image', href: 'images/logo.png' },
      ],
    },
    pageTransition: { name: 'page', mode: 'out-in' },
  },
  modules: ['./modules/icon-reload', '@nuxt/ui', '@vueuse/nuxt', '@nuxt/eslint'],
  icon: {
    provider: 'none',
    fallbackToApi: false,
    clientBundle: {
      scan: {
        globInclude: ['app/**/*.{vue,ts,js}', 'node_modules/@nuxt/ui/dist/shared/ui*.mjs'],
        globExclude: ['dist', 'build', 'coverage', 'test', 'tests', '.*'],
      },
    },
  },
  nitro: {
    sourceMap: false === isProd,
    externals: {
      inline: ['h3'],
    },
    output: {
      publicDir: isProd ? __dirname + '/exported' : __dirname + '/dist',
    },
    ...extraNitro,
  },
  vite: {
    optimizeDeps: {
      include: ['@vue/devtools-core', '@vue/devtools-kit', '@vue-flow/core'],
    },
    server: {
      allowedHosts: true,
    },
    build: {
      chunkSizeWarningLimit: 550,
      rollupOptions: {
        onwarn(warning, warn) {
          if ('SOURCEMAP_BROKEN' === warning.code || 'PLUGIN_TIMINGS' === warning.code) {
            return;
          }

          warn(warning);
        },
      },
    },
  },
  telemetry: false,
  compatibilityDate: '2025-08-03',
  experimental: {
    checkOutdatedBuildInterval: 1000 * 60 * 60,
    payloadExtraction: 'client',
    defaults: {
      nuxtLink: {
        prefetchOn: {
          interaction: true,
          visibility: false,
        },
      },
    },
  },
  typescript: {
    typeCheck: true,
  },
});
