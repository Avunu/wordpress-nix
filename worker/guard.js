/**
 * Frontend hardening: the public plane must never expose wp-admin or other
 * attack surface that belongs to the Zero-Trust-gated backend. These rules
 * run before the page cache and before the container sees the request.
 *
 * The Worker-Assets tree (lib/static-assets.nix) already contains no
 * wp-admin/ and no .php files, so nothing here can be shadowed by a static
 * asset served ahead of the Worker.
 */

/** wp-admin endpoints that legitimately serve anonymous frontend features. */
const ADMIN_PASSTHROUGH = new Set( [
	'/wp-admin/admin-ajax.php',
	'/wp-admin/admin-post.php',
] );

/** Auth signals that allow a request past the REST users guard. */
const AUTH_COOKIE = /(^|;\s*)wordpress_(logged_in|sec)_/;

/**
 * Apply the hardening rules to a request.
 *
 * @param {Request} request The incoming request.
 * @param {object}  env     The Worker environment (uses env.BACKEND_ADMIN_URL).
 * @returns {Response|null} A blocking/redirect response, or null to continue.
 */
export function guard( request, env ) {
	const url = new URL( request.url );
	const path = url.pathname;

	// Direct PHP execution inside wp-content/wp-includes: classic backdoor
	// and exploit vector. All legitimate statics come from Worker Assets.
	if ( /^\/wp-(content|includes)\/.*\.ph(p\d?|tml)$/i.test( path ) ) {
		return new Response( 'Not Found', { status: 404 } );
	}

	if ( path === '/xmlrpc.php' ) {
		return new Response( 'Forbidden', { status: 403 } );
	}

	// Trackback spam, multisite endpoints (N/A), and external cron pokes
	// (cron runs on the backend plane).
	if (
		path === '/wp-trackback.php' ||
		path === '/wp-signup.php' ||
		path === '/wp-activate.php' ||
		path === '/wp-cron.php'
	) {
		return new Response( 'Not Found', { status: 404 } );
	}

	if ( path === '/wp-admin' || path.startsWith( '/wp-admin/' ) ) {
		if ( ADMIN_PASSTHROUGH.has( path ) ) {
			return null;
		}
		// Transition-only escape hatch for sites that have not stood up the
		// Zero-Trust admin backend yet (e.g. mid-migration). The cache still
		// bypasses wp-admin; remove the var as soon as the backend exists.
		if ( env.GUARD_ALLOW_WP_ADMIN === '1' ) {
			return null;
		}
		// Send editors to the real admin behind Cloudflare Access. The
		// target is discoverable anyway and Access-gated, so the redirect
		// leaks nothing; a 404 would just generate support tickets.
		if ( env.BACKEND_ADMIN_URL ) {
			const target = new URL( env.BACKEND_ADMIN_URL );
			target.pathname = path === '/wp-admin' ? '/wp-admin/' : path;
			target.search = url.search;
			return Response.redirect( target.toString(), 302 );
		}
		return new Response( 'Not Found', { status: 404 } );
	}

	// Anonymous user enumeration via the REST users endpoint. Everything
	// else under /wp-json passes through: WordPress enforces auth and
	// nonces on mutations, and block themes/Woo rely on the REST API.
	const restRoute = url.searchParams.get( 'rest_route' ) ?? '';
	if (
		/^\/wp-json\/wp\/v2\/users(\/|$)/.test( path ) ||
		restRoute.startsWith( '/wp/v2/users' )
	) {
		const cookie = request.headers.get( 'cookie' ) ?? '';
		if ( ! AUTH_COOKIE.test( cookie ) && ! request.headers.has( 'authorization' ) ) {
			return Response.json(
				{ code: 'rest_unauthorized', message: 'Authentication required.' },
				{ status: 401 }
			);
		}
	}

	return null;
}
