<?php
/*
Plugin Name: Performance Keys
Plugin URI: https://avu.nu/
Description: Composite keys on the core meta and posts tables for the SQLite-backed engines, added once through the driver so the schema stays coherent.
Version: 1.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

/*
 * What index-wp-mysql-for-speed did for MySQL, for the platform's SQLite
 * engines (local file, Turso, D1). That plugin's central move -- making the
 * composite key the InnoDB clustered primary key -- has no SQLite analogue
 * (a table is clustered by rowid), and its rewritten primary keys are what
 * restore-core-keys undoes on migration. Its secondary composites transfer,
 * and the measurements say so: a front-page meta_query on the first
 * migrated site went from 600 ms to 37 ms with (meta_key, post_id) on
 * wp_postmeta. WordPress's own keys are meta_key alone and post_id alone,
 * so a join filtered on both scans one side.
 *
 * The keys are added with ALTER TABLE ... ADD KEY through the driver, never
 * with raw CREATE INDEX, so the driver's information schema knows them and a
 * later table rebuild keeps them. That only works well once the driver
 * creates indexes in place (wordpress-sqlite-anywhere 1.2+): before that an
 * ALTER TABLE rebuilt the table, which on Turso means copying wp_postmeta
 * through the quadratic AUTOINCREMENT path. So the feature stays off on an
 * older driver, and off on MySQL, where the operator has other tooling.
 *
 * Runs on the admin plane (admin_init, cron) only: the public plane never
 * writes. One option records the applied version; bump PLATFORM_PERF_KEYS
 * to add keys.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

const PLATFORM_PERF_KEYS_VERSION = 1;
const PLATFORM_PERF_KEYS_OPTION  = 'platform_performance_keys';

/**
 * The keys, per unprefixed table: key name => column list, as MySQL DDL.
 *
 * @return array<string, array<string, string>>
 */
function wp_platform_performance_keys(): array {
	return array(
		'postmeta'    => array( 'meta_key_post_id' => 'meta_key(191), post_id' ),
		'usermeta'    => array( 'meta_key_user_id' => 'meta_key(191), user_id' ),
		'termmeta'    => array( 'meta_key_term_id' => 'meta_key(191), term_id' ),
		'commentmeta' => array( 'meta_key_comment_id' => 'meta_key(191), comment_id' ),
		'posts'       => array(
			'post_parent_type_status' => 'post_parent, post_type, post_status',
			'author_type_status_date' => 'post_author, post_type, post_status, post_date',
		),
	);
}

/**
 * Whether this site runs on a driver that adds keys in place.
 */
function wp_platform_performance_keys_supported(): bool {
	if ( ! defined( 'DB_ENGINE' ) || 'mysql' === DB_ENGINE ) {
		return false;
	}
	return class_exists( 'WP_MySQL_On_SQLite', false )
		&& method_exists( 'WP_MySQL_On_SQLite', 'is_index_only_alter_table' );
}

/**
 * Add the keys that are missing, once per version.
 */
function wp_platform_apply_performance_keys(): void {
	global $wpdb;
	if ( (int) get_option( PLATFORM_PERF_KEYS_OPTION, 0 ) >= PLATFORM_PERF_KEYS_VERSION ) {
		return;
	}
	if ( ! wp_platform_performance_keys_supported() ) {
		return;
	}

	$failed = false;
	foreach ( wp_platform_performance_keys() as $table => $keys ) {
		$table_name = $wpdb->prefix . $table;
		$existing   = $wpdb->get_col( "SHOW INDEX FROM `{$table_name}`", 2 ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
		if ( ! is_array( $existing ) || $wpdb->last_error ) {
			// Not a table this site has (or not readable): nothing to do here.
			continue;
		}
		foreach ( $keys as $key => $columns ) {
			if ( in_array( $key, $existing, true ) ) {
				continue;
			}
			$wpdb->suppress_errors( true );
			$result = $wpdb->query( "ALTER TABLE `{$table_name}` ADD KEY `{$key}` ({$columns})" ); // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
			$wpdb->suppress_errors( false );
			// On an embedded replica the index list above may lag a write made
			// by an earlier request; the primary then answers "Duplicate key
			// name", which means the key is there.
			if ( false === $result && false === stripos( (string) $wpdb->last_error, 'Duplicate key name' ) ) {
				$failed = true;
				error_log( sprintf( 'platform-performance-keys: could not add %s.%s: %s', $table_name, $key, wp_strip_all_tags( (string) $wpdb->last_error ) ) ); // phpcs:ignore WordPress.PHP.DevelopmentFunctions.error_log_error_log
			}
		}
	}
	if ( ! $failed ) {
		update_option( PLATFORM_PERF_KEYS_OPTION, PLATFORM_PERF_KEYS_VERSION, false );
	}
}
add_action( 'admin_init', 'wp_platform_apply_performance_keys' );
add_action(
	'init',
	static function (): void {
		if ( wp_doing_cron() ) {
			wp_platform_apply_performance_keys();
		}
	}
);
