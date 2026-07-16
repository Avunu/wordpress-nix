import { fileURLToPath } from 'node:url';
import { defineWorkersConfig } from '@cloudflare/vitest-pool-workers/config';

// The D1 proxy handler comes from the sqlite-database-integration project.
// Locally a sibling checkout is used; CI materializes the flake input
// (nix build .#sqlite-driver-src) and points this env var at it, so tests
// run against the exact pinned revision.
const d1ProxySrc =
	process.env.WP_SQLITE_D1_PROXY_SRC ??
	fileURLToPath(
		new URL(
			'../../sqlite-database-integration/packages/d1-proxy-worker/src/handler.js',
			import.meta.url
		)
	);

export default defineWorkersConfig( {
	resolve: {
		alias: {
			'@wp-sqlite/d1-proxy-worker': d1ProxySrc,
		},
	},
	test: {
		poolOptions: {
			workers: {
				isolatedStorage: true,
				miniflare: {
					compatibilityDate: '2026-06-01',
					kvNamespaces: [ 'CACHE_KV' ],
					d1Databases: [ 'DB' ],
					bindings: {
						CACHE_PURGE_SECRET: 'test-secret',
						D1_PROXY_TOKEN: 'test-d1-token',
						BACKEND_ADMIN_URL: 'https://site.avunu.io',
					},
				},
			},
		},
	},
} );
