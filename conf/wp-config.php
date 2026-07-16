<?php
// Database settings
define('DB_HOST', getenv('WORDPRESS_DB_HOST') ?: 'mysql');
define('DB_USER', getenv('WORDPRESS_DB_USER') ?: 'wordpress');
define('DB_PASSWORD', getenv('WORDPRESS_DB_PASSWORD') ?: 'wordpress');
define('DB_NAME', getenv('WORDPRESS_DB_NAME') ?: 'wordpress');
define('DB_CHARSET', 'utf8');
define('DB_COLLATE', '');

$table_prefix = getenv('WORDPRESS_TABLE_PREFIX') ?: 'wp_';

// Authentication Unique Keys and Salts
require_once('/var/www/html/wp-salts.php');

// Generic env-to-constant mapping: any WPCONF_<NAME>=<value> environment
// variable becomes define('<NAME>', <value>), with 'true'/'false' coerced
// to booleans. This is how per-plane behavior (DISALLOW_FILE_MODS,
// DISABLE_WP_CRON) and plugin constants (S3_*, CLOUDFLARE_EMAIL_*,
// JWT_AUTH_*) reach WordPress. Runs before this file's own defaults so a
// deployment can override them.
foreach (getenv() as $wpconf_key => $wpconf_value) {
    if (strpos($wpconf_key, 'WPCONF_') === 0) {
        $wpconf_name = substr($wpconf_key, 7);
        if ($wpconf_name !== '' && !defined($wpconf_name)) {
            if ($wpconf_value === 'true') {
                $wpconf_value = true;
            } elseif ($wpconf_value === 'false') {
                $wpconf_value = false;
            }
            define($wpconf_name, $wpconf_value);
        }
    }
}
unset($wpconf_key, $wpconf_value, $wpconf_name);

// Debug mode
defined('WP_DEBUG') || define('WP_DEBUG', !!getenv('WORDPRESS_DEBUG', '') );

// Extra WordPress configs
if ($extra = getenv('WORDPRESS_CONFIG_EXTRA')) {
    eval($extra);
}

// If we're behind a proxy server and using HTTPS, we need to alert WordPress of that fact
if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {
    $_SERVER['HTTPS'] = 'on';
}

defined('WP_HOME') || define('WP_HOME', getenv('WORDPRESS_HOME'));
defined('WP_SITEURL') || define('WP_SITEURL', getenv('WORDPRESS_SITE_URL'));
defined('FS_METHOD') || define('FS_METHOD', 'direct');
// Core is pinned and baked into the image; it never self-updates.
defined('WP_AUTO_UPDATE_CORE') || define('WP_AUTO_UPDATE_CORE', false);
defined('AUTOMATIC_UPDATER_DISABLED') || define('AUTOMATIC_UPDATER_DISABLED', true);
defined('CONCATENATE_SCRIPTS') || define('CONCATENATE_SCRIPTS', false);
defined('DISALLOW_FILE_EDIT') || define('DISALLOW_FILE_EDIT', true);
defined('DISABLE_WP_CRON') || define('DISABLE_WP_CRON', true);
defined('WP_CACHE') || define('WP_CACHE', true);
defined('WP_POST_REVISIONS') || define('WP_POST_REVISIONS', 5);
defined('EMPTY_TRASH_DAYS') || define('EMPTY_TRASH_DAYS', 7);
defined('WP_MEMORY_LIMIT') || define('WP_MEMORY_LIMIT', '1G');

/* Add any custom values between this line and the "stop editing" line. */



/* That's all, stop editing! Happy publishing. */

/** Absolute path to the WordPress directory. */
if ( ! defined( 'ABSPATH' ) ) {
	define( 'ABSPATH', __DIR__ . '/' );
}

/** Sets up WordPress vars and included files. */
require_once ABSPATH . 'wp-settings.php';
