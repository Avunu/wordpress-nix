<?php
/*
Plugin Name: Environment
Plugin URI: https://avu.nu/
Description: Keeps production-only plugins (security, anti-spam, remote management) out of non-production environments, without touching the database.
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
	return array_values( array_filter( array_map( 'trim', explode( ',', (string) WP_PLATFORM_PRODUCTION_ONLY_PLUGINS ) ) ) );
}

/**
 * Filter the active plugin list.
 *
 * @param mixed $plugins The option value.
 * @return mixed
 */
function wp_platform_filter_active_plugins( $plugins ) {
	$disabled = wp_platform_production_only_plugins();
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
			'<div class="notice notice-info"><p>%s <code>%s</code></p></div>',
			esc_html( sprintf( 'This is a %s environment; production-only plugins are not loaded:', wp_get_environment_type() ) ),
			esc_html( implode( '</code>, <code>', $disabled ) ) // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- escaped per item, tags are ours.
		);
	}
);
