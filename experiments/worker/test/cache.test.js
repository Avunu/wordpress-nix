import { createExecutionContext, env, waitOnExecutionContext } from 'cloudflare:test';
import { beforeEach, describe, expect, it } from 'vitest';
import {
	buildCacheKey,
	handlePurge,
	resetVersionMemo,
	servePageCache,
	shouldBypass,
} from '../cache.js';

beforeEach( () => {
	// The version is memoized in a module global (not storage), so reset it
	// between tests; isolatedStorage handles KV and the cache.
	resetVersionMemo();
} );

function get( path, headers = {} ) {
	return new Request( `https://example.com${ path }`, { method: 'GET', headers } );
}

/**
 * An origin that returns a fixed HTML response and counts invocations.
 */
function countingOrigin( init = {} ) {
	const state = { calls: 0 };
	const origin = async () => {
		state.calls += 1;
		return new Response( init.body ?? '<html>ok</html>', {
			status: init.status ?? 200,
			headers: { 'content-type': 'text/html', ...( init.headers ?? {} ) },
		} );
	};
	return { origin, state };
}

async function serve( request, origin ) {
	const ctx = createExecutionContext();
	const response = await servePageCache( request, env, ctx, origin );
	await waitOnExecutionContext( ctx ); // let ctx.waitUntil(cache.put) settle
	return response;
}

describe( 'shouldBypass', () => {
	it( 'caches anonymous GET pages', () => {
		expect( shouldBypass( get( '/' ) ) ).toBe( false );
		expect( shouldBypass( get( '/hello-world/' ) ) ).toBe( false );
	} );

	it( 'bypasses non-GET methods', () => {
		expect( shouldBypass( new Request( 'https://example.com/', { method: 'POST' } ) ) ).toBe( true );
	} );

	it( 'bypasses admin, login, REST, cron, xmlrpc', () => {
		for ( const p of [ '/wp-admin/', '/wp-admin/edit.php', '/wp-login.php', '/wp-json/wp/v2/posts', '/wp-cron.php', '/xmlrpc.php', '/wp-comments-post.php' ] ) {
			expect( shouldBypass( get( p ) ), p ).toBe( true );
		}
	} );

	it( 'bypasses dynamic query params', () => {
		expect( shouldBypass( get( '/?rest_route=/wp/v2/posts' ) ) ).toBe( true );
		expect( shouldBypass( get( '/page/?preview=true' ) ) ).toBe( true );
		expect( shouldBypass( get( '/?customize_changeset_uuid=abc' ) ) ).toBe( true );
	} );

	it( 'bypasses requests carrying a WordPress auth/session cookie', () => {
		expect( shouldBypass( get( '/', { cookie: 'wordpress_logged_in_9a=alice|...' } ) ) ).toBe( true );
		expect( shouldBypass( get( '/', { cookie: 'foo=1; wp-postpass_1a=x' } ) ) ).toBe( true );
		expect( shouldBypass( get( '/', { cookie: 'woocommerce_cart_hash=abc' } ) ) ).toBe( true );
	} );

	it( 'does not bypass on unrelated cookies', () => {
		expect( shouldBypass( get( '/', { cookie: 'cf_clearance=x; _hp2_id=y' } ) ) ).toBe( false );
	} );
} );

describe( 'buildCacheKey', () => {
	it( 'strips tracking params and folds in the version', () => {
		const a = buildCacheKey( get( '/p/?utm_source=nl&id=2&gclid=x' ), '5' );
		const b = buildCacheKey( get( '/p/?id=2' ), '5' );
		const au = new URL( a.url );
		expect( au.searchParams.get( 'id' ) ).toBe( '2' );
		expect( au.searchParams.get( 'utm_source' ) ).toBe( null );
		expect( au.searchParams.get( '__wpcv' ) ).toBe( '5' );
		// Same page, different tracking params → identical key.
		expect( new URL( a.url ).pathname ).toBe( new URL( b.url ).pathname );
		expect( au.searchParams.get( 'id' ) ).toBe( new URL( b.url ).searchParams.get( 'id' ) );
	} );

	it( 'produces a different key for a different version', () => {
		const v1 = buildCacheKey( get( '/p/' ), '1' ).url;
		const v2 = buildCacheKey( get( '/p/' ), '2' ).url;
		expect( v1 ).not.toBe( v2 );
	} );
} );

describe( 'servePageCache', () => {
	it( 'MISS then HIT: the origin is hit once', async () => {
		const { origin, state } = countingOrigin();
		const r1 = await serve( get( '/miss-hit/' ), origin );
		expect( r1.headers.get( 'cf-cache-status' ) ).toBe( 'MISS' );

		const r2 = await serve( get( '/miss-hit/' ), origin );
		expect( r2.headers.get( 'cf-cache-status' ) ).toBe( 'HIT' );
		expect( await r2.text() ).toBe( '<html>ok</html>' );
		expect( state.calls ).toBe( 1 ); // second served from cache
	} );

	it( 'does not cache responses that set cookies', async () => {
		const { origin, state } = countingOrigin( { headers: { 'set-cookie': 'x=1' } } );
		const r1 = await serve( get( '/cookie/' ), origin );
		expect( r1.headers.get( 'cf-cache-status' ) ).toBe( 'BYPASS' );
		await serve( get( '/cookie/' ), origin );
		expect( state.calls ).toBe( 2 ); // never cached
	} );

	it( 'honors X-WP-Cacheable: 0', async () => {
		const { origin, state } = countingOrigin( { headers: { 'x-wp-cacheable': '0' } } );
		await serve( get( '/optout/' ), origin );
		await serve( get( '/optout/' ), origin );
		expect( state.calls ).toBe( 2 );
	} );

	it( 'strips the internal X-WP-Cacheable header from the client response', async () => {
		const { origin } = countingOrigin( { headers: { 'x-wp-cacheable': '1' } } );
		const r = await serve( get( '/strip/' ), origin );
		expect( r.headers.get( 'x-wp-cacheable' ) ).toBe( null );
	} );

	it( 'does not cache non-200 or non-HTML responses', async () => {
		const html404 = countingOrigin( { status: 404 } );
		await serve( get( '/e404/' ), html404.origin );
		await serve( get( '/e404/' ), html404.origin );
		expect( html404.state.calls ).toBe( 2 );

		const json = { origin: async () => new Response( '{}', { status: 200, headers: { 'content-type': 'application/json' } } ), calls: 0 };
		let n = 0;
		const jsonOrigin = async () => ( n++, new Response( '{}', { status: 200, headers: { 'content-type': 'application/json' } } ) );
		await serve( get( '/json/' ), jsonOrigin );
		await serve( get( '/json/' ), jsonOrigin );
		expect( n ).toBe( 2 );
	} );

	it( 'a version bump turns a HIT back into a MISS', async () => {
		const { origin } = countingOrigin();
		await serve( get( '/purge-me/' ), origin );
		expect( ( await serve( get( '/purge-me/' ), origin ) ).headers.get( 'cf-cache-status' ) ).toBe( 'HIT' );

		const purge = await handlePurge(
			new Request( 'https://example.com/__cache/purge', {
				method: 'POST',
				headers: { 'x-wp-cache-secret': 'test-secret' },
			} ),
			env
		);
		expect( purge.status ).toBe( 200 );
		expect( ( await purge.json() ).purged ).toBe( true );

		expect( ( await serve( get( '/purge-me/' ), origin ) ).headers.get( 'cf-cache-status' ) ).toBe( 'MISS' );
	} );
} );

describe( 'handlePurge', () => {
	it( 'rejects a wrong secret', async () => {
		const res = await handlePurge(
			new Request( 'https://example.com/__cache/purge', { method: 'POST', headers: { 'x-wp-cache-secret': 'nope' } } ),
			env
		);
		expect( res.status ).toBe( 401 );
	} );

	it( 'rejects a missing secret', async () => {
		const res = await handlePurge( new Request( 'https://example.com/__cache/purge', { method: 'POST' } ), env );
		expect( res.status ).toBe( 401 );
	} );

	it( 'rejects non-POST', async () => {
		const res = await handlePurge( new Request( 'https://example.com/__cache/purge', { method: 'GET' } ), env );
		expect( res.status ).toBe( 405 );
	} );
} );
