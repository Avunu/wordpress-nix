<?php
/*
Plugin Name: Stop User Enumeration
Plugin URI: https://avu.nu/
Description: Keeps usernames off the public surface: author-id probes, the users REST endpoint, the users sitemap and oEmbed author URLs.
Version: 1.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

/*
 * The platform equivalent of the stop-user-enumeration plugin's core, with no
 * settings and no per-request writes (that plugin re-registers its uninstall
 * hook on every load, rewriting the uninstall_plugins option each time --
 * a WAN round trip per page view on a remote database).
 *
 * Logged-in users are unaffected: the block editor's author picker needs the
 * users endpoint, and editors may browse ?author= archives.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Refuse ?author=<n> probes from anonymous visitors.
 *
 * WordPress redirects ?author=1 to /author/<login>/, which reveals the login
 * for each user id in turn. Named values (?author=name) are left alone.
 */
function wp_platform_block_author_id_probe(): void {
	if ( is_user_logged_in() || ! isset( $_REQUEST['author'] ) ) { // phpcs:ignore WordPress.Security.NonceVerification
		return;
	}
	$author = wp_unslash( $_REQUEST['author'] ); // phpcs:ignore WordPress.Security.NonceVerification
	if ( is_array( $author ) || 1 === preg_match( '/\d/', (string) $author ) ) {
		wp_die( 'Forbidden', 'Forbidden', array( 'response' => 403 ) );
	}
}
add_action( 'init', 'wp_platform_block_author_id_probe' );

/**
 * The users REST endpoints answer authenticated requests only.
 *
 * Done on the routes' own permission callbacks rather than rest_pre_dispatch:
 * a plugin that returns null from that filter regardless of what it received
 * (there is one on the first migrated site) would silently undo a short-circuit.
 *
 * @param array $endpoints The registered routes.
 * @return array
 */
function wp_platform_users_rest_requires_auth( $endpoints ) {
	foreach ( $endpoints as $route => $handlers ) {
		if ( 1 !== preg_match( '#^/wp/v\d+/users(?:/|$)#i', $route ) || ! is_array( $handlers ) ) {
			continue;
		}
		foreach ( $handlers as $key => $handler ) {
			if ( ! is_array( $handler ) || ! array_key_exists( 'callback', $handler ) ) {
				continue;
			}
			$permission = $handler['permission_callback'] ?? null;
			$endpoints[ $route ][ $key ]['permission_callback'] = static function ( $request ) use ( $permission ) {
				if ( ! is_user_logged_in() ) {
					return new WP_Error(
						'rest_cannot_access',
						'Only authenticated users can access the users endpoint.',
						array( 'status' => rest_authorization_required_code() )
					);
				}
				return is_callable( $permission ) ? call_user_func( $permission, $request ) : true;
			};
		}
	}
	return $endpoints;
}
add_filter( 'rest_endpoints', 'wp_platform_users_rest_requires_auth', PHP_INT_MAX );

/**
 * No users sitemap: it lists every author's public URL.
 *
 * @param WP_Sitemaps_Provider $provider The provider.
 * @param string               $name     Its name.
 * @return WP_Sitemaps_Provider|false
 */
function wp_platform_drop_users_sitemap( $provider, $name ) {
	return 'users' === $name ? false : $provider;
}
add_filter( 'wp_sitemaps_add_provider', 'wp_platform_drop_users_sitemap', 10, 2 );

/**
 * No author URL in oEmbed responses.
 *
 * @param array $data The oEmbed response data.
 * @return array
 */
function wp_platform_strip_oembed_author_url( $data ) {
	if ( is_array( $data ) ) {
		unset( $data['author_url'] );
	}
	return $data;
}
add_filter( 'oembed_response_data', 'wp_platform_strip_oembed_author_url' );
