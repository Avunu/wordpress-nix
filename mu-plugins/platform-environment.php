<?php
/*
Plugin Name: Environment
Plugin URI: https://avu.nu/
Description: wordpress-nix's runtime posture for a read-only Bedrock site: environment-only configuration, plugins kept to their environments, no update checks, two-factor for administrators, no application passwords, no executable uploads, and no mail outside production.
Version: 3.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

/*
 * Everything here reads Bedrock's environment (getenv: the services.wordpress-nix
 * `settings` and `environmentFiles`, or the dev shell's), never the database, so a
 * database that moves between environments carries nothing environment-specific.
 *
 *   WORDPRESS_NIX_PRODUCTION_ONLY_PLUGINS   plugin directories loaded only in
 *   WORDPRESS_NIX_DEVELOPMENT_ONLY_PLUGINS  production / development, comma-separated
 *   WORDPRESS_NIX_APPLICATION_PASSWORDS     true allows application passwords
 *   WORDPRESS_NIX_ACCESS_ADMIN_PATHS        true: admin and login requests need
 *                                           Cloudflare Access's assertion
 *   WORDPRESS_NIX_HOME_FROM_REQUEST         true, outside production: home and
 *                                           siteurl follow the request's host (a preview)
 *   WORDPRESS_NIX_MAIL_CATCHER              true: a non-production environment
 *                                           whose mail is caught (the dev shell's Mailpit)
 *   WC_LOG_HANDLER, S3_DIRECT_UPLOADS,      constants, when set and not yet defined
 *   DISABLE_WP_CRON
 *   WP_MAX_MEMORY_LIMIT                     admin and image requests' memory_limit
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

/**
 * An environment variable, or null when it is unset or empty.
 *
 * @param string $name Variable name.
 * @return string|null
 */
function wp_platform_env( string $name ): ?string {
	$value = getenv( $name );
	if ( false === $value || '' === $value ) {
		$value = $_ENV[ $name ] ?? $_SERVER[ $name ] ?? null; // phpcs:ignore WordPress.Security.ValidatedSanitizedInput -- environment, not request input.
	}
	return ( null === $value || '' === $value ) ? null : (string) $value;
}

/**
 * An environment variable as a boolean (true, 1, yes, on).
 *
 * @param string $name Variable name.
 * @return bool
 */
function wp_platform_env_flag( string $name ): bool {
	return in_array( strtolower( (string) wp_platform_env( $name ) ), array( '1', 'true', 'yes', 'on' ), true );
}

/**
 * Whether code and core are read-only here: Bedrock's DISALLOW_FILE_MODS,
 * true outside development.
 *
 * @return bool
 */
function wp_platform_is_read_only(): bool {
	return defined( 'DISALLOW_FILE_MODS' ) && DISALLOW_FILE_MODS;
}

/*
 * ---- the constants bridge ---------------------------------------------------
 *
 * config/application.php stays Bedrock's own, so the few constants plugins read
 * that Bedrock does not define come from the environment here. Each is read late
 * enough: DISABLE_WP_CRON in wp_cron(), WC_LOG_HANDLER when WooCommerce makes its
 * logger, S3_DIRECT_UPLOADS when wp-cloud-files enqueues its uploader.
 */
foreach ( array( 'DISABLE_WP_CRON', 'S3_DIRECT_UPLOADS' ) as $wp_platform_constant ) {
	if ( ! defined( $wp_platform_constant ) && null !== wp_platform_env( $wp_platform_constant ) ) {
		define( $wp_platform_constant, wp_platform_env_flag( $wp_platform_constant ) );
	}
}
if ( ! defined( 'WC_LOG_HANDLER' ) && null !== wp_platform_env( 'WC_LOG_HANDLER' ) ) {
	define( 'WC_LOG_HANDLER', wp_platform_env( 'WC_LOG_HANDLER' ) );
}
unset( $wp_platform_constant );

/*
 * WP_MAX_MEMORY_LIMIT is defined by core before any plugin loads, so it cannot
 * be bridged as a constant; wp_raise_memory_limit() filters it per context.
 */
if ( null !== wp_platform_env( 'WP_MAX_MEMORY_LIMIT' ) ) {
	$wp_platform_max_memory = static function (): string {
		return (string) wp_platform_env( 'WP_MAX_MEMORY_LIMIT' );
	};
	add_filter( 'admin_memory_limit', $wp_platform_max_memory );
	add_filter( 'image_memory_limit', $wp_platform_max_memory );
	unset( $wp_platform_max_memory );
}

/*
 * ---- plugins kept to their environment -----------------------------------------
 *
 * Filtered out of active_plugins at load time, so the option in the database is
 * untouched and carries the production state wherever it goes. Production-only:
 * security, anti-spam, remote-management and CAPTCHA plugins with no environment
 * switch of their own. Development-only: WPCodeBox, whose generated plugin
 * production cannot write.
 */

/**
 * Parse a comma-separated list of plugin directories.
 *
 * @param string|null $value The list.
 * @return string[]
 */
function wp_platform_split_plugin_list( ?string $value ): array {
	return array_values( array_filter( array_map( 'trim', explode( ',', (string) $value ) ) ) );
}

/**
 * The plugin directories kept out of this environment.
 *
 * @return string[]
 */
function wp_platform_disabled_plugins(): array {
	$environment = wp_get_environment_type();
	$disabled    = array();
	if ( 'production' !== $environment ) {
		$disabled = wp_platform_split_plugin_list( wp_platform_env( 'WORDPRESS_NIX_PRODUCTION_ONLY_PLUGINS' ) );
	}
	if ( ! in_array( $environment, array( 'development', 'local' ), true ) ) {
		$disabled = array_merge( $disabled, wp_platform_split_plugin_list( wp_platform_env( 'WORDPRESS_NIX_DEVELOPMENT_ONLY_PLUGINS' ) ) );
	}
	return array_values( array_unique( $disabled ) );
}

/**
 * Filter the active plugin list.
 *
 * @param mixed $plugins The option value.
 * @return mixed
 */
function wp_platform_filter_active_plugins( $plugins ) {
	$disabled = wp_platform_disabled_plugins();
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

add_action(
	'admin_notices',
	static function (): void {
		$disabled = wp_platform_disabled_plugins();
		if ( array() === $disabled ) {
			return;
		}
		printf(
			'<div class="notice notice-info"><p>%s %s</p></div>',
			esc_html( sprintf( 'This is a %s environment; these plugins are not loaded here:', wp_get_environment_type() ) ),
			implode( ', ', array_map( static fn( string $plugin ): string => '<code>' . esc_html( $plugin ) . '</code>', $disabled ) ) // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- escaped per item, tags are ours.
		);
	}
);

/*
 * ---- no update checks on a read-only tree --------------------------------------
 *
 * Nothing can be updated in place: code moves through composer.lock. So no
 * outbound calls to wordpress.org for it, no nag, and no Site Health findings about
 * auto-updates or a writable upgrade directory that cannot apply here.
 */
if ( wp_platform_is_read_only() ) {
	$wp_platform_no_updates = static function () {
		return (object) array(
			'last_checked'    => time(),
			'version_checked' => get_bloginfo( 'version' ),
			'updates'         => array(),
			'response'        => array(),
			'no_update'       => array(),
			'translations'    => array(),
			'checked'         => array(),
		);
	};
	foreach ( array( 'update_core', 'update_plugins', 'update_themes' ) as $wp_platform_transient ) {
		add_filter( "pre_site_transient_{$wp_platform_transient}", $wp_platform_no_updates );
	}
	unset( $wp_platform_no_updates, $wp_platform_transient );

	add_filter(
		'site_status_tests',
		static function ( array $tests ): array {
			unset(
				$tests['async']['background_updates'],
				$tests['direct']['plugin_theme_auto_updates'],
				$tests['direct']['update_temp_backup_writable'],
				$tests['direct']['available_updates_disk_space']
			);
			return $tests;
		}
	);
}

/*
 * ---- two-factor for administrators (production) --------------------------------
 *
 * The two-factor plugin (wp-plugin/two-factor, in every site's composer.json)
 * makes it opt-in per user. In production a user who can manage_options always has
 * the Email provider enabled, so a stolen password alone is not a session. The
 * filter is the plugin's own (verified against 0.10.0,
 * class-two-factor-core.php: two_factor_enabled_providers_for_user, applied with
 * the enabled list and the user ID; the Email provider is available to every user).
 */
if ( 'production' === wp_get_environment_type() ) {
	add_filter(
		'two_factor_enabled_providers_for_user',
		static function ( $enabled, $user_id ) {
			$enabled = is_array( $enabled ) ? $enabled : array();
			if ( user_can( (int) $user_id, 'manage_options' ) && ! in_array( 'Two_Factor_Email', $enabled, true ) ) {
				$enabled[] = 'Two_Factor_Email';
			}
			return $enabled;
		},
		PHP_INT_MAX,
		2
	);
}

/*
 * ---- application passwords ------------------------------------------------------
 *
 * A login that skips two-factor. Off unless the site asks for them.
 */
if ( ! wp_platform_env_flag( 'WORDPRESS_NIX_APPLICATION_PASSWORDS' ) ) {
	add_filter( 'wp_is_application_passwords_available', '__return_false' );
}

/*
 * ---- no executable uploads -------------------------------------------------------
 *
 * The web server never runs PHP under uploads, whatever the name; this keeps such
 * files out of the media library and the bucket in the first place.
 */

/**
 * Whether a file name has an executable extension anywhere in it (x.php.jpg too).
 *
 * @param string $name File name.
 * @return bool
 */
function wp_platform_is_executable_name( string $name ): bool {
	return 1 === preg_match( '/\.(ph(p\d?|tml|ar|ps|t)|cgi|pl|py|sh|htaccess|user\.ini)(\.|$)/i', $name );
}

add_filter(
	'wp_handle_upload_prefilter',
	static function ( $file ) {
		if ( is_array( $file ) && wp_platform_is_executable_name( (string) ( $file['name'] ?? '' ) ) ) {
			$file['error'] = 'Executable files cannot be uploaded.';
		}
		return $file;
	}
);
add_filter( 'wp_handle_sideload_prefilter', static fn( $file ) => apply_filters( 'wp_handle_upload_prefilter', $file ) );

/*
 * ---- no mail outside production --------------------------------------------------
 *
 * A preview or staging copy holds production's users and orders: it must not mail
 * them. The dev shell catches mail in Mailpit and says so.
 */
if ( 'production' !== wp_get_environment_type() && ! wp_platform_env_flag( 'WORDPRESS_NIX_MAIL_CATCHER' ) ) {
	add_filter(
		'pre_wp_mail',
		static function ( $short_circuit, $atts ) {
			// phpcs:ignore WordPress.PHP.DevelopmentFunctions.error_log_error_log -- the journal is the record.
			error_log( sprintf( 'wordpress-nix: mail not sent outside production: %s', is_array( $atts ) ? (string) ( $atts['subject'] ?? '' ) : '' ) );
			return true;
		},
		PHP_INT_MAX,
		2
	);
}

/*
 * ---- a preview's home is the request's host ---------------------------------------
 *
 * A preview slot answers under a hostname known only when the preview is made.
 * Never in production, where WP_HOME is the site's.
 */
if ( 'production' !== wp_get_environment_type() && wp_platform_env_flag( 'WORDPRESS_NIX_HOME_FROM_REQUEST' ) && ! empty( $_SERVER['HTTP_HOST'] ) ) {
	$wp_platform_request_home = ( is_ssl() ? 'https://' : 'http://' ) . strtolower( preg_replace( '/[^A-Za-z0-9.:\-\[\]]/', '', (string) wp_unslash( $_SERVER['HTTP_HOST'] ) ) ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput.InputNotSanitized -- reduced to host characters.
	add_filter( 'option_home', static fn() => $wp_platform_request_home, 20 );
	add_filter( 'option_siteurl', static fn() => $wp_platform_request_home . '/wp', 20 );
	unset( $wp_platform_request_home );
}

/*
 * ---- path-scoped Cloudflare Access --------------------------------------------------
 *
 * When the site's admin paths are behind an Access application, a request that
 * reached them without Access's assertion went around the edge: refuse it. The
 * edge verifies the assertion; this only refuses its absence. Front-end endpoints
 * under wp-admin stay open.
 */
if ( wp_platform_env_flag( 'WORDPRESS_NIX_ACCESS_ADMIN_PATHS' ) && PHP_SAPI !== 'cli' && empty( $_SERVER['HTTP_CF_ACCESS_JWT_ASSERTION'] ) ) {
	$wp_platform_path = (string) wp_parse_url( (string) ( $_SERVER['REQUEST_URI'] ?? '' ), PHP_URL_PATH ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput -- compared, never output.
	if ( 1 === preg_match( '#/wp-(admin/|login\.php)#', $wp_platform_path )
		&& 0 === preg_match( '#/wp-admin/(admin-ajax|admin-post|load-styles|load-scripts)\.php$#', $wp_platform_path ) ) {
		status_header( 403 );
		exit( 'Forbidden' );
	}
	unset( $wp_platform_path );
}
