<?php
/**
 * APCu Object Cache Drop-in.
 *
 * A persistent WordPress object cache backed by APCu, with a per-request
 * in-memory layer. It is loaded from wp-content/object-cache.php by the
 * WordPress container image.
 *
 * Persistent groups are stored in APCu (shared across all requests handled
 * by a container instance); non-persistent groups live only for the current
 * request. This dramatically reduces the number of database round trips,
 * which is the dominant cost when the database is remote (e.g. Cloudflare
 * D1 over the SQLite driver's HTTP transport).
 *
 * Disable by setting the environment variable WORDPRESS_OBJECT_CACHE=none,
 * which prevents the image from installing this drop-in.
 *
 * @package wordpress-nix
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

// Fall back to the default (non-persistent) cache when APCu is unavailable
// (e.g. on the CLI without apc.enable_cli, or if the extension is missing).
if ( ! function_exists( 'apcu_fetch' ) || ! apcu_enabled() ) {
	return;
}

/**
 * Adds data to the cache, if the cache key doesn't already exist.
 *
 * @see WP_Object_Cache::add()
 */
function wp_cache_add( $key, $data, $group = '', $expire = 0 ) {
	global $wp_object_cache;
	return $wp_object_cache->add( $key, $data, $group, (int) $expire );
}

/**
 * Adds multiple values to the cache in one call.
 *
 * @see WP_Object_Cache::add_multiple()
 */
function wp_cache_add_multiple( array $data, $group = '', $expire = 0 ) {
	global $wp_object_cache;
	return $wp_object_cache->add_multiple( $data, $group, (int) $expire );
}

/**
 * Replaces the contents of the cache with new data.
 *
 * @see WP_Object_Cache::replace()
 */
function wp_cache_replace( $key, $data, $group = '', $expire = 0 ) {
	global $wp_object_cache;
	return $wp_object_cache->replace( $key, $data, $group, (int) $expire );
}

/**
 * Saves the data to the cache.
 *
 * @see WP_Object_Cache::set()
 */
function wp_cache_set( $key, $data, $group = '', $expire = 0 ) {
	global $wp_object_cache;
	return $wp_object_cache->set( $key, $data, $group, (int) $expire );
}

/**
 * Sets multiple values to the cache in one call.
 *
 * @see WP_Object_Cache::set_multiple()
 */
function wp_cache_set_multiple( array $data, $group = '', $expire = 0 ) {
	global $wp_object_cache;
	return $wp_object_cache->set_multiple( $data, $group, (int) $expire );
}

/**
 * Retrieves the cache contents from the cache by key and group.
 *
 * @see WP_Object_Cache::get()
 */
function wp_cache_get( $key, $group = '', $force = false, &$found = null ) {
	global $wp_object_cache;
	return $wp_object_cache->get( $key, $group, $force, $found );
}

/**
 * Retrieves multiple values from the cache in one call.
 *
 * @see WP_Object_Cache::get_multiple()
 */
function wp_cache_get_multiple( $keys, $group = '', $force = false ) {
	global $wp_object_cache;
	return $wp_object_cache->get_multiple( $keys, $group, $force );
}

/**
 * Removes the cache contents matching key and group.
 *
 * @see WP_Object_Cache::delete()
 */
function wp_cache_delete( $key, $group = '' ) {
	global $wp_object_cache;
	return $wp_object_cache->delete( $key, $group );
}

/**
 * Deletes multiple values from the cache in one call.
 *
 * @see WP_Object_Cache::delete_multiple()
 */
function wp_cache_delete_multiple( array $keys, $group = '' ) {
	global $wp_object_cache;
	return $wp_object_cache->delete_multiple( $keys, $group );
}

/**
 * Increments numeric cache item's value.
 *
 * @see WP_Object_Cache::incr()
 */
function wp_cache_incr( $key, $offset = 1, $group = '' ) {
	global $wp_object_cache;
	return $wp_object_cache->incr( $key, (int) $offset, $group );
}

/**
 * Decrements numeric cache item's value.
 *
 * @see WP_Object_Cache::decr()
 */
function wp_cache_decr( $key, $offset = 1, $group = '' ) {
	global $wp_object_cache;
	return $wp_object_cache->decr( $key, (int) $offset, $group );
}

/**
 * Removes all cache items.
 *
 * @see WP_Object_Cache::flush()
 */
function wp_cache_flush() {
	global $wp_object_cache;
	return $wp_object_cache->flush();
}

/**
 * Removes all cache items from the in-memory runtime cache.
 *
 * @see WP_Object_Cache::flush_runtime()
 */
function wp_cache_flush_runtime() {
	global $wp_object_cache;
	return $wp_object_cache->flush_runtime();
}

/**
 * Removes all cache items in a group.
 *
 * @see WP_Object_Cache::flush_group()
 */
function wp_cache_flush_group( $group ) {
	global $wp_object_cache;
	return $wp_object_cache->flush_group( $group );
}

/**
 * Determines whether the object cache implementation supports a feature.
 *
 * @param string $feature The feature to check for.
 * @return bool True if the feature is supported, false otherwise.
 */
function wp_cache_supports( $feature ) {
	switch ( $feature ) {
		case 'add_multiple':
		case 'set_multiple':
		case 'get_multiple':
		case 'delete_multiple':
		case 'flush_runtime':
		case 'flush_group':
			return true;
		default:
			return false;
	}
}

/**
 * Closes the cache.
 *
 * @return true Always returns true.
 */
function wp_cache_close() {
	return true;
}

/**
 * Sets up Object Cache Global and assigns it.
 *
 * @global WP_Object_Cache $wp_object_cache
 */
function wp_cache_init() {
	$GLOBALS['wp_object_cache'] = new WP_Object_Cache();
}

/**
 * Adds a group or set of groups to the list of global groups.
 *
 * @see WP_Object_Cache::add_global_groups()
 */
function wp_cache_add_global_groups( $groups ) {
	global $wp_object_cache;
	$wp_object_cache->add_global_groups( $groups );
}

/**
 * Adds a group or set of groups to the list of non-persistent groups.
 *
 * @see WP_Object_Cache::add_non_persistent_groups()
 */
function wp_cache_add_non_persistent_groups( $groups ) {
	global $wp_object_cache;
	$wp_object_cache->add_non_persistent_groups( $groups );
}

/**
 * Switches the internal blog ID.
 *
 * @see WP_Object_Cache::switch_to_blog()
 */
function wp_cache_switch_to_blog( $blog_id ) {
	global $wp_object_cache;
	$wp_object_cache->switch_to_blog( $blog_id );
}

/**
 * Reset internal cache keys and structures.
 *
 * @deprecated Use wp_cache_switch_to_blog().
 */
function wp_cache_reset() {
	// Deprecated in WordPress; retained for compatibility.
}

/**
 * Core class that implements an APCu-backed object cache.
 */
class WP_Object_Cache {
	/**
	 * The in-request cache: group => ( key => value ).
	 *
	 * @var array
	 */
	private $cache = array();

	/**
	 * Groups that are never persisted to APCu.
	 *
	 * @var array
	 */
	private $non_persistent_groups = array();

	/**
	 * Groups shared across all sites of a network.
	 *
	 * @var array
	 */
	private $global_groups = array();

	/**
	 * The blog prefix applied to non-global cache keys.
	 *
	 * @var string
	 */
	private $blog_prefix = '';

	/**
	 * Whether this is a multisite install.
	 *
	 * @var bool
	 */
	private $multisite = false;

	/**
	 * A stable key prefix isolating this install's APCu namespace.
	 *
	 * @var string
	 */
	private $key_salt = '';

	/**
	 * Cache hits during the request.
	 *
	 * @var int
	 */
	public $cache_hits = 0;

	/**
	 * Cache misses during the request.
	 *
	 * @var int
	 */
	public $cache_misses = 0;

	/**
	 * Constructor.
	 */
	public function __construct() {
		$this->multisite   = function_exists( 'is_multisite' ) && is_multisite();
		$this->blog_prefix = $this->multisite ? get_current_blog_id() . ':' : '';

		// Isolate the APCu namespace per install, so that flushes and keys
		// never collide with another WordPress on the same APCu instance.
		$salt = defined( 'WP_CACHE_KEY_SALT' ) ? WP_CACHE_KEY_SALT : '';
		if ( '' === $salt && defined( 'AUTH_KEY' ) ) {
			$salt = AUTH_KEY;
		}
		$this->key_salt = 'wp:' . substr( md5( $salt . ABSPATH ), 0, 12 ) . ':';
	}

	/**
	 * Build the full APCu key for a cache key and group.
	 *
	 * @param string $key   The cache key.
	 * @param string $group The cache group.
	 * @return string       The APCu key.
	 */
	private function build_key( $key, $group ) {
		$prefix = isset( $this->global_groups[ $group ] ) ? '' : $this->blog_prefix;
		return $this->key_salt . $prefix . $group . ':' . $key;
	}

	/**
	 * Normalize a group name.
	 *
	 * @param string $group The group.
	 * @return string       The normalized group.
	 */
	private function normalize_group( $group ) {
		return empty( $group ) ? 'default' : $group;
	}

	/**
	 * Whether a group is persisted to APCu.
	 *
	 * @param string $group The group.
	 * @return bool         True when the group is persistent.
	 */
	private function is_persistent_group( $group ) {
		return ! isset( $this->non_persistent_groups[ $group ] );
	}

	/**
	 * Adds data to the cache if the key does not already exist.
	 */
	public function add( $key, $data, $group = 'default', $expire = 0 ) {
		if ( function_exists( 'wp_suspend_cache_addition' ) && wp_suspend_cache_addition() ) {
			return false;
		}
		$group = $this->normalize_group( $group );

		// Present in the runtime cache means it exists.
		if ( isset( $this->cache[ $group ] ) && array_key_exists( $key, $this->cache[ $group ] ) ) {
			return false;
		}

		if ( $this->is_persistent_group( $group ) ) {
			$full = $this->build_key( $key, $group );
			if ( ! apcu_add( $full, $data, $expire ) ) {
				// Another request already stored it; mirror it locally.
				$this->cache[ $group ][ $key ] = apcu_fetch( $full );
				return false;
			}
		}

		$this->cache[ $group ][ $key ] = is_object( $data ) ? clone $data : $data;
		return true;
	}

	/**
	 * Adds multiple values to the cache.
	 */
	public function add_multiple( array $data, $group = 'default', $expire = 0 ) {
		$values = array();
		foreach ( $data as $key => $value ) {
			$values[ $key ] = $this->add( $key, $value, $group, $expire );
		}
		return $values;
	}

	/**
	 * Replaces the contents of the cache with new data, if the key exists.
	 */
	public function replace( $key, $data, $group = 'default', $expire = 0 ) {
		$group = $this->normalize_group( $group );

		$found = false;
		$this->get( $key, $group, true, $found );
		if ( ! $found ) {
			return false;
		}
		return $this->set( $key, $data, $group, $expire );
	}

	/**
	 * Saves data to the cache.
	 */
	public function set( $key, $data, $group = 'default', $expire = 0 ) {
		$group = $this->normalize_group( $group );

		if ( $this->is_persistent_group( $group ) ) {
			apcu_store( $this->build_key( $key, $group ), $data, $expire );
		}

		$this->cache[ $group ][ $key ] = is_object( $data ) ? clone $data : $data;
		return true;
	}

	/**
	 * Sets multiple values to the cache.
	 */
	public function set_multiple( array $data, $group = 'default', $expire = 0 ) {
		$values = array();
		foreach ( $data as $key => $value ) {
			$values[ $key ] = $this->set( $key, $value, $group, $expire );
		}
		return $values;
	}

	/**
	 * Retrieves the cache contents, if it exists.
	 */
	public function get( $key, $group = 'default', $force = false, &$found = null ) {
		$group = $this->normalize_group( $group );

		// Serve from the runtime cache unless a forced re-read is requested.
		if (
			! $force
			&& isset( $this->cache[ $group ] )
			&& array_key_exists( $key, $this->cache[ $group ] )
		) {
			$found = true;
			$this->cache_hits += 1;
			$value = $this->cache[ $group ][ $key ];
			return is_object( $value ) ? clone $value : $value;
		}

		if ( ! $this->is_persistent_group( $group ) ) {
			$found = false;
			$this->cache_misses += 1;
			return false;
		}

		$value = apcu_fetch( $this->build_key( $key, $group ), $found );
		if ( $found ) {
			$this->cache[ $group ][ $key ] = $value;
			$this->cache_hits += 1;
			return is_object( $value ) ? clone $value : $value;
		}

		$this->cache_misses += 1;
		return false;
	}

	/**
	 * Retrieves multiple values from the cache.
	 */
	public function get_multiple( $keys, $group = 'default', $force = false ) {
		$values = array();
		foreach ( $keys as $key ) {
			$values[ $key ] = $this->get( $key, $group, $force );
		}
		return $values;
	}

	/**
	 * Removes the contents of the cache key in the group.
	 */
	public function delete( $key, $group = 'default' ) {
		$group = $this->normalize_group( $group );

		if ( $this->is_persistent_group( $group ) ) {
			apcu_delete( $this->build_key( $key, $group ) );
		}
		unset( $this->cache[ $group ][ $key ] );
		return true;
	}

	/**
	 * Deletes multiple values from the cache.
	 */
	public function delete_multiple( array $keys, $group = 'default' ) {
		$values = array();
		foreach ( $keys as $key ) {
			$values[ $key ] = $this->delete( $key, $group );
		}
		return $values;
	}

	/**
	 * Increments a numeric cache item's value.
	 */
	public function incr( $key, $offset = 1, $group = 'default' ) {
		$group  = $this->normalize_group( $group );
		$offset = (int) $offset;

		if ( $this->is_persistent_group( $group ) ) {
			$full = $this->build_key( $key, $group );
			if ( ! apcu_exists( $full ) ) {
				return false;
			}
			$value = apcu_inc( $full, $offset );
			if ( false !== $value && $value < 0 ) {
				$value = 0;
				apcu_store( $full, $value );
			}
			$this->cache[ $group ][ $key ] = $value;
			return $value;
		}

		if ( ! isset( $this->cache[ $group ] ) || ! array_key_exists( $key, $this->cache[ $group ] ) ) {
			return false;
		}
		$value = (int) $this->cache[ $group ][ $key ] + $offset;
		$value = max( 0, $value );
		$this->cache[ $group ][ $key ] = $value;
		return $value;
	}

	/**
	 * Decrements a numeric cache item's value.
	 */
	public function decr( $key, $offset = 1, $group = 'default' ) {
		$group  = $this->normalize_group( $group );
		$offset = (int) $offset;

		if ( $this->is_persistent_group( $group ) ) {
			$full = $this->build_key( $key, $group );
			if ( ! apcu_exists( $full ) ) {
				return false;
			}
			// APCu clamps at 0-floor manually to match core semantics.
			$current = (int) apcu_fetch( $full );
			$value   = max( 0, $current - $offset );
			apcu_store( $full, $value );
			$this->cache[ $group ][ $key ] = $value;
			return $value;
		}

		if ( ! isset( $this->cache[ $group ] ) || ! array_key_exists( $key, $this->cache[ $group ] ) ) {
			return false;
		}
		$value = max( 0, (int) $this->cache[ $group ][ $key ] - $offset );
		$this->cache[ $group ][ $key ] = $value;
		return $value;
	}

	/**
	 * Clears the object cache of all data.
	 */
	public function flush() {
		$this->cache = array();
		// Single-tenant container: clearing the whole APCu user cache is
		// correct and simplest. Keys are namespaced per install regardless.
		return apcu_clear_cache();
	}

	/**
	 * Removes all cache items from the in-memory runtime cache.
	 */
	public function flush_runtime() {
		$this->cache      = array();
		$this->cache_hits = 0;
		$this->cache_misses = 0;
		return true;
	}

	/**
	 * Removes all cache items in a group.
	 */
	public function flush_group( $group ) {
		$group = $this->normalize_group( $group );
		unset( $this->cache[ $group ] );

		if ( ! $this->is_persistent_group( $group ) ) {
			return true;
		}

		// Delete every APCu entry whose key belongs to this group.
		$prefix   = $this->build_key( '', $group );
		$iterator = new APCUIterator( '/^' . preg_quote( $prefix, '/' ) . '/', APC_ITER_KEY );
		return apcu_delete( $iterator );
	}

	/**
	 * Sets the list of global cache groups.
	 */
	public function add_global_groups( $groups ) {
		foreach ( (array) $groups as $group ) {
			$this->global_groups[ $group ] = true;
		}
	}

	/**
	 * Sets the list of non-persistent cache groups.
	 */
	public function add_non_persistent_groups( $groups ) {
		foreach ( (array) $groups as $group ) {
			$this->non_persistent_groups[ $group ] = true;
		}
	}

	/**
	 * Switches the internal blog ID.
	 */
	public function switch_to_blog( $blog_id ) {
		$blog_id           = (int) $blog_id;
		$this->blog_prefix = $this->multisite ? $blog_id . ':' : '';
	}

	/**
	 * Echoes the stats of the caching for the current request.
	 */
	public function stats() {
		$total = $this->cache_hits + $this->cache_misses;
		$ratio = $total > 0 ? round( $this->cache_hits / $total * 100, 1 ) : 0;
		echo '<p>';
		echo '<strong>Cache Hits:</strong> ' . (int) $this->cache_hits . '<br />';
		echo '<strong>Cache Misses:</strong> ' . (int) $this->cache_misses . '<br />';
		echo '<strong>Hit Ratio:</strong> ' . esc_html( $ratio ) . '%';
		echo '</p>';
	}
}
