<?php
/*
Plugin Name: Cloudflare Page Cache Helper
Plugin URI: https://avu.nu/
Description: Signals page cacheability to the Cloudflare Worker edge cache and purges it (by cache-version bump) when content changes.
Version: 1.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

/*
 * This works with the edge full-page cache in the Cloudflare Worker
 * (wordpress-cloudflare/src/cache.js). The Worker caches anonymous HTML
 * pages by default; this mu-plugin:
 *
 *   1. Marks responses that must NOT be cached for anonymous visitors
 *      (search, previews, logged-in) with the "X-WP-Cacheable: 0" header,
 *      and marks cacheable pages "1" with a friendly Cache-Control.
 *   2. On content changes, calls the Worker's /__cache/purge endpoint,
 *      which bumps a global cache version — invalidating every cached page
 *      at once — authenticated with the shared CACHE_PURGE_SECRET.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * Whether the current front-end request may be cached for anonymous visitors.
 *
 * @return bool
 */
function wp_cf_cache_is_cacheable() {
	if ( is_user_logged_in() ) {
		return false;
	}
	if ( ! isset( $_SERVER['REQUEST_METHOD'] ) || 'GET' !== $_SERVER['REQUEST_METHOD'] ) {
		return false;
	}
	// Dynamic or personalized views.
	if ( is_admin() || is_preview() || is_search() || is_customize_preview() ) {
		return false;
	}
	if ( function_exists( 'is_404' ) && is_404() ) {
		// 404s are not cached by the Worker (non-200); mark explicitly anyway.
		return false;
	}
	return true;
}

/**
 * Emit the cacheability signal header for the current front-end response.
 */
function wp_cf_cache_send_headers() {
	if ( headers_sent() ) {
		return;
	}
	if ( wp_cf_cache_is_cacheable() ) {
		header( 'X-WP-Cacheable: 1' );
		// The Worker sets its own edge TTL; this is a friendly hint and
		// keeps the response cache-consistent for any intermediary.
		header( 'Cache-Control: public, max-age=0, s-maxage=300' );
	} else {
		header( 'X-WP-Cacheable: 0' );
	}
}
// template_redirect runs on the front end after the query is parsed (so the
// conditional tags are available) and before output — and never for REST or
// admin requests, which the Worker bypasses anyway.
add_action( 'template_redirect', 'wp_cf_cache_send_headers' );

/**
 * Purge the edge cache by bumping its version, once per request.
 */
function wp_cf_cache_purge() {
	static $purged = false;
	if ( $purged ) {
		return;
	}
	$purged = true;

	$secret = getenv( 'CACHE_PURGE_SECRET' );
	if ( empty( $secret ) || ! defined( 'WP_HOME' ) || empty( WP_HOME ) ) {
		return;
	}

	wp_remote_post(
		rtrim( WP_HOME, '/' ) . '/__cache/purge',
		array(
			'headers'  => array( 'X-WP-Cache-Secret' => $secret ),
			'blocking' => false,
			'timeout'  => 2,
		)
	);
}

/*
 * Purge on content and configuration changes. A version bump invalidates
 * the whole cache, so there is no need to enumerate affected URLs.
 */
foreach (
	array(
		'save_post',
		'deleted_post',
		'trashed_post',
		'untrashed_post',
		'comment_post',
		'edit_comment',
		'wp_set_comment_status',
		'switch_theme',
		'customize_save_after',
		'activated_plugin',
		'deactivated_plugin',
		'wp_update_nav_menu',
	) as $wp_cf_cache_hook
) {
	add_action( $wp_cf_cache_hook, 'wp_cf_cache_purge', 10, 0 );
}
