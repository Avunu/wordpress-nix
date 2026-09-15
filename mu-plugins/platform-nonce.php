<?php
/*
Plugin Name: Extend Nonce Lifetime
Plugin URI: https://avu.nu/
Description: Extends the default nonce lifetime to 60 days.
Version: 1.1
Author: Avunu LLC
Author URI: https://avu.nu/
*/

// A closure, not a named function: a site that carries its own copy of this
// file (some do, from before it was a platform mu-plugin) must not fatal on a
// redeclaration.
add_filter(
	'nonce_life',
	static function () {
		return 86400 * 60; // 60 days, in seconds
	}
);
