import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';
import { handleD1Proxy } from '../d1.js';

const query = ( body, headers = {} ) =>
	new Request( 'https://example.com/__d1/v1/query', {
		method: 'POST',
		headers: { 'content-type': 'application/json', ...headers },
		body: typeof body === 'string' ? body : JSON.stringify( body ),
	} );

const authed = { authorization: 'Bearer test-d1-token' };

describe( 'handleD1Proxy', () => {
	it( 'rejects requests without a bearer token', async () => {
		const res = await handleD1Proxy( query( { sql: 'SELECT 1' } ), env );
		expect( res.status ).toBe( 401 );
	} );

	it( 'rejects a wrong token', async () => {
		const res = await handleD1Proxy(
			query( { sql: 'SELECT 1' }, { authorization: 'Bearer wrong' } ),
			env
		);
		expect( res.status ).toBe( 401 );
	} );

	it( 'is a 404 when no token is configured (endpoint off)', async () => {
		const res = await handleD1Proxy( query( { sql: 'SELECT 1' }, authed ), {
			...env,
			D1_PROXY_TOKEN: undefined,
		} );
		expect( res.status ).toBe( 404 );
	} );

	it( 'rejects oversized bodies', async () => {
		const huge = JSON.stringify( { sql: 'SELECT 1', params: [ 'x'.repeat( 2 << 20 ) ] } );
		const res = await handleD1Proxy( query( huge, authed ), env );
		expect( res.status ).toBe( 413 );
	} );

	it( 'executes an authenticated query against the D1 binding', async () => {
		const res = await handleD1Proxy( query( { sql: 'SELECT 1 AS one', params: [] }, authed ), env );
		expect( res.status ).toBe( 200 );
		const body = await res.json();
		expect( body.success ).toBe( true );
	} );

	it( 'threads D1 session bookmarks through', async () => {
		const res = await handleD1Proxy(
			query( { sql: 'SELECT 1', params: [], session: { constraint: 'first-unconstrained' } }, authed ),
			env
		);
		expect( res.status ).toBe( 200 );
	} );
} );
