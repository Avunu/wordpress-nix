import { env } from 'cloudflare:test';
import { describe, expect, it } from 'vitest';
import { guard } from '../guard.js';

const req = ( path, init = {} ) => new Request( `https://example.com${ path }`, init );

describe( 'guard', () => {
	it( 'redirects wp-admin to the backend admin URL, preserving path and query', () => {
		const res = guard( req( '/wp-admin/edit.php?post_type=page' ), env );
		expect( res.status ).toBe( 302 );
		expect( res.headers.get( 'location' ) ).toBe(
			'https://site.avunu.io/wp-admin/edit.php?post_type=page'
		);
	} );

	it( 'redirects bare /wp-admin with a trailing slash', () => {
		const res = guard( req( '/wp-admin' ), env );
		expect( res.status ).toBe( 302 );
		expect( res.headers.get( 'location' ) ).toBe( 'https://site.avunu.io/wp-admin/' );
	} );

	it( '404s wp-admin when no backend URL is configured', () => {
		const res = guard( req( '/wp-admin/index.php' ), { ...env, BACKEND_ADMIN_URL: undefined } );
		expect( res.status ).toBe( 404 );
	} );

	it( 'passes admin-ajax.php and admin-post.php through', () => {
		expect( guard( req( '/wp-admin/admin-ajax.php' ), env ) ).toBeNull();
		expect( guard( req( '/wp-admin/admin-post.php' ), env ) ).toBeNull();
	} );

	it( 'forbids xmlrpc.php', () => {
		expect( guard( req( '/xmlrpc.php' ), env ).status ).toBe( 403 );
	} );

	it( '404s trackback, signup, activate, and external cron', () => {
		for ( const path of [ '/wp-trackback.php', '/wp-signup.php', '/wp-activate.php', '/wp-cron.php' ] ) {
			expect( guard( req( path ), env ).status ).toBe( 404 );
		}
	} );

	it( '404s direct PHP under wp-content and wp-includes', () => {
		expect( guard( req( '/wp-content/uploads/shell.php' ), env ).status ).toBe( 404 );
		expect( guard( req( '/wp-content/plugins/x/x.phtml' ), env ).status ).toBe( 404 );
		expect( guard( req( '/wp-includes/load.php' ), env ).status ).toBe( 404 );
	} );

	it( 'blocks anonymous REST user enumeration, both path and rest_route forms', () => {
		expect( guard( req( '/wp-json/wp/v2/users' ), env ).status ).toBe( 401 );
		expect( guard( req( '/wp-json/wp/v2/users/1' ), env ).status ).toBe( 401 );
		expect( guard( req( '/?rest_route=/wp/v2/users' ), env ).status ).toBe( 401 );
	} );

	it( 'allows authenticated REST user requests through', () => {
		const cookie = req( '/wp-json/wp/v2/users', {
			headers: { cookie: 'wordpress_logged_in_abc=1' },
		} );
		expect( guard( cookie, env ) ).toBeNull();
		const bearer = req( '/wp-json/wp/v2/users', {
			headers: { authorization: 'Bearer token' },
		} );
		expect( guard( bearer, env ) ).toBeNull();
	} );

	it( 'passes wp-admin through when the transition escape hatch is set', () => {
		expect( guard( req( '/wp-admin/index.php' ), { ...env, GUARD_ALLOW_WP_ADMIN: '1' } ) ).toBeNull();
		// Hard blocks are not affected by the escape hatch.
		expect( guard( req( '/xmlrpc.php' ), { ...env, GUARD_ALLOW_WP_ADMIN: '1' } ).status ).toBe( 403 );
	} );

	it( 'leaves ordinary REST and page requests alone', () => {
		expect( guard( req( '/wp-json/wp/v2/posts' ), env ) ).toBeNull();
		expect( guard( req( '/2026/07/hello-world/' ), env ) ).toBeNull();
		expect( guard( req( '/wp-login.php' ), env ) ).toBeNull();
		expect( guard( req( '/wp-comments-post.php' ), env ) ).toBeNull();
	} );
} );
