/**
 * Edge full-page cache for WordPress, built on the Workers Cache API.
 *
 * A cache hit is served entirely from the edge and never reaches the
 * container, PHP, or D1. Anonymous HTML pages are cached by default; API,
 * admin, and any request carrying a WordPress auth/session cookie always
 * flow through to WordPress.
 *
 * Invalidation is global and instant via a cache "version" stored in KV:
 * the version is part of every cache key, so bumping it (on content change,
 * see the /__cache/purge endpoint) makes every colo miss at once. Old
 * entries then expire on their own TTL. This sidesteps the Cache API's
 * per-colocation scope, which has no global wildcard delete.
 */

import { timingSafeEqual } from './auth.js';

/** Default edge TTL for cached pages, in seconds. */
const DEFAULT_TTL = 300;

/** Paths that must always reach WordPress (never cached). */
const BYPASS_PATH = new RegExp(
	'^/(' +
		[
			'wp-admin(/|$)',
			'wp-login\\.php',
			'wp-cron\\.php',
			'wp-json(/|$)',
			'xmlrpc\\.php',
			'wp-comments-post\\.php',
			'wp-trackback\\.php',
			'wp-signup\\.php',
			'wp-activate\\.php',
		].join('|') +
		')'
);

/** Query parameters that indicate a dynamic (never-cached) request. */
const BYPASS_QUERY = [ 'rest_route', 'preview', 'customize_changeset_uuid', 'unapproved', 'replytocom' ];

/**
 * Cookies that indicate a logged-in / personalized visitor. Matching the
 * prefix anywhere in the Cookie header bypasses the cache.
 */
const BYPASS_COOKIE = new RegExp(
	'(^|;\\s*)(' +
		[
			'wordpress_logged_in_',
			'wp-postpass_',
			'comment_author_',
			'wordpress_sec_',
			'woocommerce_items_in_cart',
			'woocommerce_cart_hash',
			'wp_woocommerce_session_',
		].join('|') +
		')'
);

/** Tracking query parameters stripped from the cache key. */
const TRACKING_PARAM = /^(utm_|fbclid$|gclid$|mc_|_ga$|ref$|_hsenc$|_hsmi$)/;

/**
 * Whether a request must bypass the cache and go straight to WordPress.
 *
 * @param {Request} request The incoming request.
 * @returns {boolean} True when the request must not be served from cache.
 */
export function shouldBypass( request ) {
	if ( request.method !== 'GET' && request.method !== 'HEAD' ) {
		return true;
	}

	const url = new URL( request.url );
	if ( BYPASS_PATH.test( url.pathname ) ) {
		return true;
	}
	if ( BYPASS_QUERY.some( ( param ) => url.searchParams.has( param ) ) ) {
		return true;
	}

	const cookie = request.headers.get( 'cookie' );
	if ( cookie && BYPASS_COOKIE.test( cookie ) ) {
		return true;
	}

	return false;
}

/**
 * Build the cache key for a request at a given cache version.
 *
 * Tracking parameters are dropped and the remaining query is sorted so that
 * equivalent URLs share a cache entry. The version is folded into the key,
 * so a version bump invalidates every entry at once.
 *
 * @param {Request} request The incoming request.
 * @param {string}  version The current cache version.
 * @returns {Request} A synthetic GET request used as the cache key.
 */
export function buildCacheKey( request, version ) {
	const url = new URL( request.url );
	for ( const param of [ ...url.searchParams.keys() ] ) {
		if ( TRACKING_PARAM.test( param ) ) {
			url.searchParams.delete( param );
		}
	}
	url.searchParams.sort();
	url.searchParams.set( '__wpcv', version );
	return new Request( url.toString(), { method: 'GET' } );
}

// The cache version, memoized per isolate to bound KV reads.
let versionMemo = { value: null, at: 0 };
const VERSION_MEMO_TTL_MS = 10_000;

/**
 * Get the current cache version, memoized within the isolate.
 *
 * @param {object} env The Worker environment (expects env.CACHE_KV).
 * @returns {Promise<string>} The current cache version ('0' when unset/unavailable).
 */
export async function getCacheVersion( env ) {
	if ( ! env.CACHE_KV ) {
		return '0';
	}
	const now = Date.now();
	if ( versionMemo.value !== null && now - versionMemo.at < VERSION_MEMO_TTL_MS ) {
		return versionMemo.value;
	}
	try {
		const value = ( await env.CACHE_KV.get( 'cache:version' ) ) ?? '0';
		versionMemo = { value, at: now };
		return value;
	} catch {
		// On a KV read failure, reuse the last known version, or disable
		// versioning (treat as '0') rather than failing the request.
		return versionMemo.value ?? '0';
	}
}

/**
 * Reset the isolate's memoized version (after a purge in this isolate).
 */
export function resetVersionMemo() {
	versionMemo = { value: null, at: 0 };
}

/**
 * Whether an origin response for a (non-bypassed) request may be cached.
 *
 * @param {Request}  request  The incoming request.
 * @param {Response} response The origin response.
 * @returns {boolean} True when the response should be stored.
 */
function isCacheable( request, response ) {
	if ( request.method !== 'GET' ) {
		return false;
	}
	if ( response.status !== 200 ) {
		return false;
	}
	if ( ! ( response.headers.get( 'content-type' ) || '' ).includes( 'text/html' ) ) {
		return false;
	}
	// Never cache a response that sets cookies (personalized) or that
	// WordPress explicitly marked non-cacheable.
	if ( response.headers.has( 'set-cookie' ) ) {
		return false;
	}
	if ( response.headers.get( 'x-wp-cacheable' ) === '0' ) {
		return false;
	}
	// Honor explicit no-store / private; "no-cache" and "max-age=0" are
	// overridden, since anonymous pages are cached by default.
	const cacheControl = ( response.headers.get( 'cache-control' ) || '' ).toLowerCase();
	if ( cacheControl.includes( 'no-store' ) || cacheControl.includes( 'private' ) ) {
		return false;
	}
	return true;
}

/**
 * Return a copy of a response with an added CF-Cache-Status header and the
 * internal X-WP-Cacheable header removed.
 *
 * @param {Response} response The response to copy.
 * @param {string}   status   The CF-Cache-Status value.
 * @returns {Response} The client-facing response.
 */
function withCacheStatus( response, status ) {
	const headers = new Headers( response.headers );
	headers.delete( 'x-wp-cacheable' );
	headers.set( 'cf-cache-status', status );
	return new Response( response.body, {
		status: response.status,
		statusText: response.statusText,
		headers,
	} );
}

/**
 * Serve a request through the page cache.
 *
 * @param {Request}  request The incoming request (already known non-bypass).
 * @param {object}   env     The Worker environment.
 * @param {object}   ctx     The execution context (for waitUntil).
 * @param {(req: Request) => Promise<Response>} origin Fetches from the container.
 * @returns {Promise<Response>} The response.
 */
export async function servePageCache( request, env, ctx, origin ) {
	const cache = caches.default;
	const version = await getCacheVersion( env );
	const key = buildCacheKey( request, version );

	const hit = await cache.match( key, { ignoreMethod: true } );
	if ( hit ) {
		return withCacheStatus( hit, 'HIT' );
	}

	const response = await origin( request );
	if ( ! isCacheable( request, response ) ) {
		return withCacheStatus( response, 'BYPASS' );
	}

	// Prepare the stored copy: no cookies, an explicit edge TTL, and a
	// timestamp for future stale-while-revalidate handling.
	const cacheHeaders = new Headers( response.headers );
	cacheHeaders.delete( 'set-cookie' );
	cacheHeaders.delete( 'x-wp-cacheable' );
	cacheHeaders.set( 'cache-control', `public, s-maxage=${ DEFAULT_TTL }` );
	cacheHeaders.set( 'x-wp-cached-at', Date.now().toString() );

	const stored = new Response( response.clone().body, {
		status: response.status,
		statusText: response.statusText,
		headers: cacheHeaders,
	} );
	ctx.waitUntil( cache.put( key, stored ) );

	return withCacheStatus( response, 'MISS' );
}

/**
 * Handle POST /__cache/purge: authenticate and bump the cache version,
 * invalidating every cached page globally.
 *
 * @param {Request} request The purge request.
 * @param {object}  env     The Worker environment.
 * @returns {Promise<Response>} The purge result.
 */
export async function handlePurge( request, env ) {
	if ( request.method !== 'POST' ) {
		return new Response( 'Method Not Allowed', { status: 405 } );
	}

	const secret = env.CACHE_PURGE_SECRET;
	const provided = request.headers.get( 'x-wp-cache-secret' ) ?? '';
	if ( ! secret || ! ( await timingSafeEqual( provided, secret ) ) ) {
		return Response.json( { purged: false, error: 'unauthorized' }, { status: 401 } );
	}

	if ( ! env.CACHE_KV ) {
		return Response.json( { purged: false, error: 'cache versioning is not configured' }, { status: 501 } );
	}

	const version = Date.now().toString();
	await env.CACHE_KV.put( 'cache:version', version );
	resetVersionMemo();
	return Response.json( { purged: true, version } );
}
