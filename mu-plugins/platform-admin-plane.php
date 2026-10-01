<?php
/*
Plugin Name: Admin Plane
Plugin URI: https://avu.nu/
Description: Repairs the links that assume WP_HOME and WP_SITEURL match on a split-plane admin face.
Version: 1.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

/*
 * The admin plane declares WP_SITEURL on its private hostname but leaves
 * WP_HOME on the public one. That split is deliberate: everything WordPress
 * generates as a link to the site -- permalinks, feeds, sitemaps, and above all
 * the URLs inside outgoing email, which Action Scheduler and WooCommerce send
 * from this plane -- then names the real site rather than an internal hostname
 * nobody can reach.
 *
 * Core supports the pair natively (it is the "WordPress in its own directory"
 * case), but one thing genuinely breaks: post preview builds its URL from
 * home_url(), so it lands on the public plane, which will not grant an editor a
 * session. Previewing is the whole point of an editing plane, so the preview
 * link is moved back onto this host below.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

if ( ! defined( 'WP_PLATFORM_PLANE' ) || 'admin' !== WP_PLATFORM_PLANE ) {
	return;
}

/**
 * Move a URL from the public host onto this one.
 *
 * @param string $link Absolute URL.
 * @return string
 */
function wp_platform_admin_rehost( $link ): string {
	$link = (string) $link;
	if ( ! defined( 'WP_PLATFORM_PUBLIC_URL' ) || ! defined( 'WP_PLATFORM_ADMIN_URL' ) ) {
		return $link;
	}

	$public = rtrim( (string) WP_PLATFORM_PUBLIC_URL, '/' );
	$admin  = rtrim( (string) WP_PLATFORM_ADMIN_URL, '/' );
	if ( '' === $public || '' === $admin || 0 !== strpos( $link, $public ) ) {
		return $link;
	}

	return $admin . substr( $link, strlen( $public ) );
}

/*
 * Priority 99: run after anything the theme or a plugin does to the preview URL,
 * so whatever they produced still ends up on this host.
 */
add_filter( 'preview_post_link', 'wp_platform_admin_rehost', 99 );
add_filter( 'preview_page_link', 'wp_platform_admin_rehost', 99 );
