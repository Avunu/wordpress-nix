/**
 * The authenticated external D1 proxy: /__d1/v1/{query,batch}.
 *
 * The stateful admin backend (on the NixOS cluster) speaks the same D1
 * proxy protocol as the in-Worker `d1.internal` outbound handler, but from
 * outside Cloudflare — so it enters here, over TLS, gated by a bearer
 * token. Authentication (timing-safe), body-size limits, and D1 session
 * bookmarks are all handled by the proxy package itself; this route only
 * wires the token, caps the body, and strips the path prefix.
 */

import { createD1ProxyHandler } from '@wp-sqlite/d1-proxy-worker';

/** Cap request bodies: admin-plane queries are small; anything huge is abuse. */
const MAX_BODY_BYTES = 1 << 20;

/**
 * Handle a /__d1/* request.
 *
 * @param {Request} request The incoming request.
 * @param {object}  env     The Worker environment (uses env.D1_PROXY_TOKEN, env.DB).
 * @returns {Promise<Response>}
 */
export async function handleD1Proxy( request, env ) {
	// Without a configured token the endpoint does not exist — never fall
	// through to an unauthenticated database.
	if ( ! env.D1_PROXY_TOKEN ) {
		return Response.json(
			{ success: false, error: { code: 'PROXY_NOT_FOUND', message: 'Not found.' } },
			{ status: 404 }
		);
	}

	// Strip the /__d1 prefix so the handler sees /v1/query | /v1/batch.
	const url = new URL( request.url );
	url.pathname = url.pathname.replace( /^\/__d1/, '' );
	return createD1ProxyHandler( () => env.DB, {
		token: env.D1_PROXY_TOKEN,
		maxBodyBytes: MAX_BODY_BYTES,
	} )( new Request( url, request ) );
}
