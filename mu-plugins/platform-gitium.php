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

/*
 * One lock for every caller.
 *
 * gitium takes an flock on a file in PHP's temp directory before it fetches,
 * merges and pushes. The web service and the platform's reconcile timer are
 * separate units, each with its own PrivateTmp, so they would take two
 * different locks and run git in the same working tree at the same time. The
 * platform therefore names the file.
 */
/**
 * Commit whatever changed, reconcile with the remote, and push.
 *
 * gitium's own entry point, gitium_auto_push(), cannot be used here. It begins
 * with
 *
 *     list( , $key ) = gitium_get_keypair();
 *     if ( ! $key ) return;
 *
 * reading the private key out of the `gitium_keypair` OPTION — and the platform
 * supplies the key as a file instead, through GIT_KEY_FILE, which
 * Git_Wrapper::get_env() prefers anyway. So that option is empty and
 * gitium_auto_push() returns before touching git: every push gitium wires to
 * wp-admin's own events is silently dead on this platform.
 *
 * Putting the key in the option to satisfy the check would be worse. Options
 * live in the database, and a split-plane site shares its database with the
 * read-only public plane — whose database user would then be able to read the
 * repository's write key.
 *
 * So this does what gitium_auto_push() does, minus the gate: group the dirty
 * working tree into one commit per plugin or theme, then hand those commits to
 * gitium's own merge-and-push so the conflict policy stays gitium's.
 *
 * @return bool Whether the push succeeded.
 */
function wp_platform_gitium_reconcile(): bool {
	if ( ! function_exists( 'gitium_merge_and_push' ) ) {
		return false;
	}
	if ( ! defined( 'GIT_KEY_FILE' ) ) {
		// Without a key there is nothing to authenticate a push with, and
		// failing loudly beats pushing nowhere on a timer.
		trigger_error( 'platform-gitium: GIT_KEY_FILE is not defined; refusing to reconcile', E_USER_WARNING );
		return false;
	}

	$commits = function_exists( 'gitium_group_commit_modified_plugins_and_themes' )
		? gitium_group_commit_modified_plugins_and_themes( '' )
		: array();

	$pushed = (bool) gitium_merge_and_push( $commits );

	if ( function_exists( 'gitium_update_versions' ) ) {
		gitium_update_versions();
	}
	return $pushed;
}

/*
 * Priority 12, so it runs after gitium's own callback at 11 has returned early.
 * The platform's timer calls the same function, so an install that happens
 * outside wp-admin is still reconciled; this only makes one inside wp-admin
 * immediate rather than waiting for the next tick.
 */
add_action( 'upgrader_process_complete', 'wp_platform_gitium_reconcile', 12, 0 );

if ( defined( 'WP_PLATFORM_GITIUM_LOCK' ) ) {
	add_filter(
		'gitium_lock_path',
		static function (): string {
			return (string) WP_PLATFORM_GITIUM_LOCK;
		}
	);
}
