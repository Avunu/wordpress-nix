<?php
/*
Plugin Name: wordpress-nix module probe
Description: Test-only (tests/module.nix): reports what PHP sees through REST, records every HTTP request that would leave, and fails `wp core update-db` (or all of wp-cli) on demand so the migrate unit's failure paths can be exercised.
*/

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

add_action(
	'rest_api_init',
	static function (): void {
		register_rest_route(
			'probe/v1',
			'/env',
			array(
				'methods'             => 'GET',
				'permission_callback' => '__return_true',
				'callback'            => static function (): array {
					return array(
						'remote_addr' => $_SERVER['REMOTE_ADDR'] ?? null,
						'https'       => is_ssl(),
						's3_bucket'   => getenv( 'S3_BUCKET' ) ?: null,
						's3_key_set'  => false !== getenv( 'S3_KEY' ) && '' !== getenv( 'S3_KEY' ),
						's3_key_md5'  => md5( (string) getenv( 'S3_KEY' ) ),
						'tmp'         => get_temp_dir(),
						'docroot_rw'  => false !== @file_put_contents( ABSPATH . 'probe.txt', 'x' ), // phpcs:ignore WordPress.PHP.NoSilencedErrors
						'uploads_rw'  => false !== @file_put_contents( wp_upload_dir()['basedir'] . '/probe.txt', 'x' ), // phpcs:ignore WordPress.PHP.NoSilencedErrors
					);
				},
			)
		);
	}
);

// The migrate-rollback scenario: change the database, then fail.
if ( defined( 'WP_CLI' ) && WP_CLI && file_exists( '/var/lib/wordpress/probe-fail-migrate' ) ) {
	WP_CLI::add_hook(
		'before_invoke:core update-db',
		static function (): void {
			update_option( 'blogname', 'Mutated by a failed migrate' );
			WP_CLI::error( 'probe: failing the migrate on purpose' );
		}
	);
}

// Every outbound request nothing short-circuited, one URL per line: last on
// pre_http_request, so it sees what the other filters left.
add_filter(
	'pre_http_request',
	static function ( $preempt, $args, $url ) {
		if ( false === $preempt ) {
			file_put_contents( '/var/lib/wordpress/probe-http.log', $url . "\n", FILE_APPEND ); // phpcs:ignore WordPress.WP.AlternativeFunctions
		}
		return $preempt;
	},
	PHP_INT_MAX,
	3
);

// WordPress that does not load (a fatal in a new build): wp-cli exits 255.
if ( defined( 'WP_CLI' ) && WP_CLI && file_exists( '/var/lib/wordpress/probe-broken' ) ) {
	exit( 255 );
}
