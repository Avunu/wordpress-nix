/**
 * WordPress on Cloudflare — the platform edge Worker.
 *
 * This module is fully site-agnostic: every site-specific value arrives
 * through `env` (wrangler vars/secrets). It is owned and versioned by
 * wordpress-nix and bundled per platform version by lib.mkSiteWorker; site
 * repos carry no copy of it.
 *
 *   Browser ── Worker ── WordPress container (FrankenPHP + SQLite driver)
 *                              │ http://d1.internal (outbound handler)
 *                        D1 proxy handler ── D1 binding ── D1
 *
 *   Backend (NixOS) ── https://<site>/__d1/v1/* (bearer) ── same handler
 *
 * The container reaches D1 with plain HTTP requests to the virtual
 * hostname `d1.internal`. The outbound handler intercepts them inside
 * the Workers runtime, where the D1 binding is available — traffic never
 * leaves Cloudflare, and the endpoint is unreachable from the internet.
 */

import { Container, getContainer } from '@cloudflare/containers';
import { createD1ProxyHandler } from '@wp-sqlite/d1-proxy-worker';
import { handlePurge, servePageCache, shouldBypass } from './cache.js';
import { handleD1Proxy } from './d1.js';
import { guard } from './guard.js';

// The outbound-handler machinery (used for the `d1.internal` route below)
// spawns this helper entrypoint via `ctx.exports.ContainerProxy`, so it
// must be a named export of the Worker's entrypoint module.
export { ContainerProxy } from '@cloudflare/containers';

export class WordPressContainer extends Container {
	defaultPort = 80;

	// Keep WordPress warm between visits.
	sleepAfter = '20m';

	// Outbound internet for plugin HTTP calls (mail API, R2 S3 API, ...).
	enableInternet = true;

	constructor( ctx, env ) {
		super( ctx, env );

		this.envVars = {
			// The D1 database drop-in. See the sqlite-database-integration
			// project's D1 backend documentation.
			WP_D1_PROXY_URL: 'http://d1.internal',
			WORDPRESS_DB_NAME: env.WORDPRESS_DB_NAME ?? 'wordpress',

			// Unused with the D1 backend, but the image's wp-config.php
			// defines the MySQL constants from these variables.
			WORDPRESS_DB_HOST: 'unused',
			WORDPRESS_DB_USER: 'unused',
			WORDPRESS_DB_PASSWORD: 'unused',

			// WordPress cannot reliably guess its public URL from inside
			// the container; pin it explicitly.
			WORDPRESS_HOME: env.WORDPRESS_URL,
			WORDPRESS_SITE_URL: env.WORDPRESS_URL,

			// Shared secret the cache-helper mu-plugin uses to authenticate
			// its purge callback to the Worker's /__cache/purge endpoint.
			CACHE_PURGE_SECRET: env.CACHE_PURGE_SECRET ?? '',

			// Salts shared with the backend plane (Worker secret). Empty
			// means the entrypoint generates local dev salts.
			WORDPRESS_SALTS: env.WORDPRESS_SALTS ?? '',
		};

		// Forward every WPCONF_* var/secret verbatim: the image's
		// wp-config.php turns each into a define(). This is how per-plane
		// behavior (DISALLOW_FILE_MODS, DISABLE_WP_CRON) and plugin
		// constants (S3_*, CLOUDFLARE_EMAIL_*, JWT_AUTH_*) are configured
		// per site without touching platform code.
		for ( const key of Object.keys( env ) ) {
			if ( key.startsWith( 'WPCONF_' ) && typeof env[ key ] === 'string' ) {
				this.envVars[ key ] = env[ key ];
			}
		}
	}
}

/*
 * Register the outbound handler for the `d1.internal` virtual host.
 *
 * This MUST be an assignment, not a `static outboundByHost = {...}` class
 * field. The containers package exposes `outboundByHost` as an inherited
 * static setter that records the handler in an internal registry; a static
 * field would instead define a shadowing own property and never call the
 * setter, leaving the handler unregistered. The container would then route
 * `d1.internal` to the public internet, which fails with HTTP 530.
 */
WordPressContainer.outboundByHost = {
	'd1.internal': ( request, env ) =>
		createD1ProxyHandler( () => env.DB, { allowInsecure: true } )( request ),
};

export default {
	/**
	 * Route a request: control endpoints, then hardening, then the page
	 * cache, then the (singleton) WordPress container.
	 *
	 * Static assets present in the Worker's assets directory are served
	 * from the edge before this handler runs (assets.run_worker_first is
	 * false), so everything reaching here is a page or dynamic request.
	 *
	 * @param {Request} request The incoming request.
	 * @param {object}  env     The Worker environment bindings.
	 * @param {object}  ctx     The execution context.
	 * @returns {Promise<Response>}
	 */
	async fetch( request, env, ctx ) {
		const url = new URL( request.url );

		if ( url.pathname === '/__cache/purge' ) {
			return handlePurge( request, env );
		}
		if ( url.pathname.startsWith( '/__d1/' ) ) {
			return handleD1Proxy( request, env );
		}

		const blocked = guard( request, env );
		if ( blocked ) {
			return blocked;
		}

		const origin = ( req ) => getContainer( env.WORDPRESS ).fetch( req );
		if ( shouldBypass( request ) ) {
			return origin( request );
		}
		return servePageCache( request, env, ctx, origin );
	},
};
