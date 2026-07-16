<?php
/*
Plugin Name: Extend Nonce Lifetime
Plugin URI: https://avu.nu/
Description: Extends the default nonce lifetime to 60 days.
Version: 1.0
Author: Avunu LLC
Author URI: https://avu.nu/
*/

function custom_nonce_lifetime() {
    return 86400 * 60; // 60 days in minutes
}
add_filter( 'nonce_life', 'custom_nonce_lifetime' );
