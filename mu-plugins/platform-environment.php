<?php
/*
Plugin Name: Environment
Plugin URI: https://avu.nu/
Description: Keeps plugins out of environments and planes where they do not belong - production-only plugins outside production, and plane-specific plugins off the other plane - without touching the database.
Version: 1.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

/*
 * Plugins such as Patchstack and CleanTalk have no environment switch of
 * their own -- Patchstack's firewall is gated on options and its ban check
 * runs regardless, CleanTalk's APBCT_IS_LOCALHOST only tweaks spam scans --
 * and in a dev shell they only cost time and write on every page view. So
 * they are filtered out of active_plugins whenever WP_ENVIRONMENT_TYPE is not
 * "production": the option in the database is untouched, so a database that
 * moves between environments carries the production state.
 *
 * The list comes from WP_PLATFORM_PRODUCTION_ONLY_PLUGINS (a comma-separated
 * list of plugin directory names), defined by the dev shell's generated
 * wp-config.php from `wordpress-nix.productionOnlyPlugins`. Undefined, or in
 * production, this file does nothing.
 *
 * The same mechanism carries the split-plane case, through
 * WP_PLATFORM_PLANE_EXCLUDED_PLUGINS (from `wordpress-nix.plane.excludePlugins`).
 * Two planes share one database, so `active_plugins` is one list for both, and a
 * plugin that belongs on only one of them has to be dropped at load time rather
 * than deactivated. Unlike the production list this one applies in production
 * too -- that is the point of it. The public plane's default drops jwt-auth,
 * which would otherwise refuse the visitor logins that plane exists to serve.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * The plugin directories kept out of this environment.
 *
 * @return string[]
 */
function wp_platform_production_only_plugins(): array {
	if ( ! defined( 'WP_PLATFORM_PRODUCTION_ONLY_PLUGINS' ) || 'production' === wp_get_environment_type() ) {
		return array();
	}
	return wp_platform_split_plugin_list( (string) WP_PLATFORM_PRODUCTION_ONLY_PLUGINS );
}

/**
 * The plugin directories that do not belong on this plane, in any environment.
 *
 * @return string[]
 */
function wp_platform_plane_excluded_plugins(): array {
	if ( ! defined( 'WP_PLATFORM_PLANE_EXCLUDED_PLUGINS' ) ) {
		return array();
	}
	return wp_platform_split_plugin_list( (string) WP_PLATFORM_PLANE_EXCLUDED_PLUGINS );
}

/**
 * Parse one of the comma-separated constants into directory names.
 *
 * @param string $value Constant value.
 * @return string[]
 */
function wp_platform_split_plugin_list( string $value ): array {
	return array_values( array_filter( array_map( 'trim', explode( ',', $value ) ) ) );
}

/**
 * Filter the active plugin list.
 *
 * @param mixed $plugins The option value.
 * @return mixed
 */
function wp_platform_filter_active_plugins( $plugins ) {
	$disabled = array_unique(
		array_merge(
			wp_platform_production_only_plugins(),
			wp_platform_plane_excluded_plugins()
		)
	);
	if ( array() === $disabled || ! is_array( $plugins ) ) {
		return $plugins;
	}
	return array_values(
		array_filter(
			$plugins,
			static function ( $plugin ) use ( $disabled ) {
				return ! in_array( dirname( (string) $plugin ), $disabled, true );
			}
		)
	);
}
add_filter( 'option_active_plugins', 'wp_platform_filter_active_plugins' );

/**
 * Say so in wp-admin, so nobody hunts for a plugin that is installed but silent.
 */
add_action(
	'admin_notices',
	static function (): void {
		$disabled = wp_platform_production_only_plugins();
		if ( array() === $disabled ) {
			return;
		}
		printf(
			'<div class="notice notice-info"><p>%s %s</p></div>',
			esc_html( sprintf( 'This is a %s environment; production-only plugins are not loaded:', wp_get_environment_type() ) ),
			implode( ', ', array_map( static fn( string $plugin ): string => '<code>' . esc_html( $plugin ) . '</code>', $disabled ) ) // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- escaped per item, tags are ours.
		);
	}
);
