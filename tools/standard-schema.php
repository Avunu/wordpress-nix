<?php
/**
 * Print WordPress's standard core-table schema (wp_get_db_schema()) for a
 * table prefix, using nothing but the core files: no database, no wp-config.
 *
 * Usage: php standard-schema.php <wordpress core dir> [prefix]
 *
 * @package wordpress-nix
 */

declare( strict_types = 1 );

$core   = rtrim( $argv[1] ?? '', '/' );
$prefix = $argv[2] ?? 'wp_';
if ( '' === $core || ! is_file( $core . '/wp-admin/includes/schema.php' ) ) {
	fwrite( STDERR, "usage: standard-schema.php <wordpress core dir> [prefix]\n" );
	exit( 1 );
}

/**
 * The slice of wpdb that wp_get_db_schema() touches.
 */
final class Schema_Wpdb { // phpcs:ignore
	public string $prefix;
	public string $base_prefix;
	public int $blogid = 1;
	public int $siteid = 1;
	public string $charset = 'utf8mb4';
	public string $collate = 'utf8mb4_unicode_ci';

	public function __construct( string $prefix ) {
		$this->prefix      = $prefix;
		$this->base_prefix = $prefix;
		foreach ( array( 'termmeta', 'terms', 'term_taxonomy', 'term_relationships', 'commentmeta', 'comments', 'links', 'options', 'postmeta', 'posts', 'users', 'usermeta', 'blogs', 'blogmeta', 'registration_log', 'signups', 'site', 'sitemeta' ) as $table ) {
			$this->$table = $prefix . $table; // phpcs:ignore
		}
	}

	public function get_charset_collate(): string {
		return 'DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci';
	}

	public function get_blog_prefix( $blog_id = null ): string { // phpcs:ignore
		return $this->prefix;
	}

	public function set_blog_id( $blog_id, $network_id = 0 ): int { // phpcs:ignore
		return 1;
	}

	public function has_cap( string $cap ): bool {
		return true;
	}
}

define( 'ABSPATH', $core . '/' );
define( 'WPINC', 'wp-includes' );
$wpdb = new Schema_Wpdb( $prefix ); // phpcs:ignore WordPress.WP.GlobalVariablesOverride.Prohibited
require ABSPATH . 'wp-includes/load.php';
require ABSPATH . 'wp-includes/functions.php';
require ABSPATH . 'wp-includes/plugin.php';
require ABSPATH . 'wp-admin/includes/schema.php';
echo wp_get_db_schema( 'all', 1 ); // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped
