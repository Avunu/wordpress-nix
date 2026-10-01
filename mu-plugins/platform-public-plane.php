<?php
/*
Plugin Name: Public Plane
Plugin URI: https://avu.nu/
Description: Makes the public plane of a split-plane site read-only in the sense that matters — nobody can administer it — while leaving ordinary visitor writes alone.
Version: 1.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

/*
 * A split-plane site serves the same database from two instances: this one on
 * the real hostname, and a private admin plane behind an identity proxy.
 *
 * The temptation is to make the public plane read-only by blocking writes --
 * a SELECT-only database user, or a path blocklist over /wp-json. Both break
 * the site: WooCommerce checkout, Give donations, comments, form submissions
 * and session handling are all writes that visitors are supposed to make, and
 * the Store API and form endpoints all live under /wp-json.
 *
 * So the rule here is about sessions, not writes: no privileged user can hold
 * one on this plane. Two things enforce it.
 *
 *   1. Each plane declares a different WP_SITEURL, so COOKIEHASH --
 *      md5(siteurl) -- differs, the auth cookie names differ, and a cookie
 *      minted on the admin host is simply not read here.
 *   2. This file refuses to authenticate a privileged user, so one cannot be
 *      minted here either.
 *
 * With no privileged session obtainable, every administrative REST route,
 * admin-ajax action and admin screen is already denied by WordPress' own
 * capability checks -- the same checks that protect a normal site. Caddy
 * removes the administration surface on top of that (see the publicPlane
 * directives in modules/nixos.nix); everything below is the belt to that
 * braces, and the part that cannot be done in Caddy.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

if ( ! defined( 'WP_PLATFORM_PLANE' ) || 'public' !== WP_PLATFORM_PLANE ) {
	return;
}

/**
 * The capability that marks a user as staff rather than a visitor.
 *
 * `edit_posts` is the boundary: administrators, editors, authors, contributors
 * and WooCommerce shop managers have it, while subscribers, customers and
 * donors do not. Those are exactly the two groups this plane separates.
 *
 * @return string
 */
function wp_platform_public_privileged_cap(): string {
	/**
	 * Filter the capability that bars a user from the public plane.
	 *
	 * @param string $cap Capability name.
	 */
	return (string) apply_filters( 'wp_platform_public_privileged_cap', 'edit_posts' );
}

/**
 * Where administration lives.
 *
 * @return string Absolute URL with no trailing slash, or '' if unconfigured.
 */
function wp_platform_admin_url_base(): string {
	if ( ! defined( 'WP_PLATFORM_ADMIN_URL' ) ) {
		return '';
	}
	return rtrim( (string) WP_PLATFORM_ADMIN_URL, '/' );
}

/**
 * Refuse to authenticate staff on the public plane.
 *
 * Priority 50 so the user object has already been resolved by core's own
 * callbacks (wp_authenticate_username_password runs at 20).
 *
 * @param null|WP_User|WP_Error $user     Candidate user.
 * @param string                $username Submitted login.
 * @param string                $password Submitted password.
 * @return null|WP_User|WP_Error
 */
function wp_platform_public_refuse_privileged( $user, $username = '', $password = '' ) {
	if ( ! $user instanceof WP_User ) {
		return $user;
	}
	if ( ! user_can( $user, wp_platform_public_privileged_cap() ) ) {
		return $user;
	}

	$admin = wp_platform_admin_url_base();
	$message = '' === $admin
		? __( 'This account must sign in on the site&#8217;s administration host.' )
		: sprintf(
			/* translators: %s: URL of the administration host. */
			__( 'This account must sign in at <a href="%s">the administration host</a>.' ),
			esc_url( $admin . '/wp-admin/' )
		);

	return new WP_Error( 'wp_platform_admin_elsewhere', $message );
}
add_filter( 'authenticate', 'wp_platform_public_refuse_privileged', 50, 3 );

/**
 * Send administration requests that still reached PHP to the admin host.
 *
 * Caddy already redirects these, so this only fires if the public plane is
 * reached some other way (a misconfigured front end, a direct request to the
 * socket). admin-ajax.php and admin-post.php are deliberately exempt: the front
 * end POSTs to both.
 */
function wp_platform_public_redirect_admin(): void {
	if ( ! is_admin() || wp_doing_ajax() ) {
		return;
	}
	if ( defined( 'WP_CLI' ) && WP_CLI ) {
		return;
	}

	$script = isset( $_SERVER['SCRIPT_NAME'] ) ? basename( (string) $_SERVER['SCRIPT_NAME'] ) : '';
	if ( in_array( $script, array( 'admin-ajax.php', 'admin-post.php' ), true ) ) {
		return;
	}

	$admin = wp_platform_admin_url_base();
	if ( '' === $admin ) {
		return;
	}

	$path = isset( $_SERVER['REQUEST_URI'] ) ? (string) $_SERVER['REQUEST_URI'] : '/wp-admin/';
	wp_redirect( $admin . $path, 302 );
	exit;
}
add_action( 'init', 'wp_platform_public_redirect_admin', 0 );

/*
 * XML-RPC has no legitimate caller on a read-only public face, and it is the
 * one authentication path that bypasses wp-login.php entirely.
 */
add_filter( 'xmlrpc_enabled', '__return_false' );

/*
 * The admin bar is nothing but links into wp-admin, which does not exist here.
 * A logged-in visitor should not be shown a toolbar of dead ends.
 */
add_action(
	'after_setup_theme',
	static function (): void {
		show_admin_bar( false );
	}
);
