<?php
/*
Plugin Name: Gitium Activation
Plugin URI: https://avu.nu/
Description: Activates the platform's own copy of gitium, so the site repo neither carries it nor records it as active.
Version: 1.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

/*
 * gitium is infrastructure, not site payload. The platform installs it into
 * wp-content/plugins/gitium on every start of a managed-mode instance and the
 * site repo's .gitignore excludes it, so it is versioned with the platform and
 * never committed into the site.
 *
 * Activating it through a filter rather than through the database matters for
 * two reasons. A split-plane site shares one database between a writable admin
 * plane and a read-only public plane, so an `active_plugins` row would load
 * gitium on the public plane too -- where the working tree is a read-only store
 * path and gitium's own requirements check would fail. And the activation hook
 * is unnecessary: GIT_KEY_FILE supplies the ssh key the hook would otherwise
 * generate into a temporary file, and the platform's init script sets the exec
 * bit on gitium's bundled ssh wrapper.
 *
 * WP_PLATFORM_GITIUM is defined by the generated wp-config.php exactly when the
 * platform installed the plugin.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

if ( ! defined( 'WP_PLATFORM_GITIUM' ) || ! WP_PLATFORM_GITIUM ) {
	return;
}

/**
 * Add the platform's gitium to the active plugin list.
 *
 * Priority 20, so it lands after platform-environment.php's removals at 10 --
 * a plane that excludes gitium is excluding a site-supplied copy, and should
 * not then have the platform's added back.
 *
 * @param mixed $plugins The option value.
 * @return mixed
 */
function wp_platform_activate_gitium( $plugins ) {
	if ( ! is_array( $plugins ) ) {
		return $plugins;
	}

	$entry = 'gitium/gitium.php';
	if ( in_array( $entry, $plugins, true ) ) {
		return $plugins;
	}
	if (
		function_exists( 'wp_platform_plane_excluded_plugins' )
		&& in_array( 'gitium', wp_platform_plane_excluded_plugins(), true )
	) {
		return $plugins;
	}
	if ( ! defined( 'WP_PLUGIN_DIR' ) || ! file_exists( WP_PLUGIN_DIR . '/' . $entry ) ) {
		return $plugins;
	}

	$plugins[] = $entry;
	return $plugins;
}
add_filter( 'option_active_plugins', 'wp_platform_activate_gitium', 20 );
