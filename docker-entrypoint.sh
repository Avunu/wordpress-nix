#!/bin/sh
set -e

# if wordpress is in maintenance mode, remove it
if [ -f /var/www/html/.maintenance ]; then
    rm /var/www/html/.maintenance
fi

# set shell as /bin/sh
export SHELL=/bin/sh
export WP_CLI_CUSTOM_SHELL=/bin/sh

# Function to run wp-cron
run_wp_cron() {
    while true; do
        echo "Running WordPress cron events"
        if ! wp cron event run --all --due-now --allow-root --path=/var/www/html; then
            echo "Error running wp-cron. Retrying in 60 seconds."
        fi
        sleep 60
    done
}

# If PROC_TYPE=worker, run cron jobs in the background
if [ "$PROC_TYPE" = "worker" ]; then
    echo "Starting wp-cron worker process"
    run_wp_cron
fi

# Function to install WordPress from the core (+ site wp-content) baked into
# the image. The image is the only source of code: no runtime downloads.
install_wordpress() {
    if [ ! -f /usr/src/wordpress/wp-includes/version.php ]; then
        echo "Error: no WordPress core baked at /usr/src/wordpress — refusing to start." >&2
        echo "Images must be built with the wordpress-nix builders (lib.mkSiteImage)." >&2
        exit 1
    fi
    echo "Installing bundled WordPress from /usr/src/wordpress"
    cp -r /usr/src/wordpress/. /var/www/html/
    chmod -R u+w /var/www/html

    # Import database if WORDPRESS_DB_URL is set
    if [ -n "${WORDPRESS_DB_URL:-}" ]; then
        import_db_wp_cli
    fi
}

# Function to import database using wp-cli
import_db_wp_cli() {
    echo "Importing database using wp-cli from: $WORDPRESS_DB_URL"
    wget -O db_dump.sql "$WORDPRESS_DB_URL"
    wp db import db_dump.sql --allow-root --path=/var/www/html
    rm db_dump.sql
}

# Function to import database using mysql cli
import_db_mysql() {
    echo "Importing database using mysql from: $WORDPRESS_DB_URL"
    wget -O db_dump.sql "$WORDPRESS_DB_URL"
    mysql -h"$WORDPRESS_DB_HOST" -u"$WORDPRESS_DB_USER" -p"$WORDPRESS_DB_PASSWORD" "$WORDPRESS_DB_NAME" < db_dump.sql
    rm db_dump.sql
}

# Function to set up the salts: from the WORDPRESS_SALTS secret when provided
# (the 8-define api.wordpress.org-format block — shared with the backend
# plane so both planes agree), otherwise generated locally. Never fetched
# from the network.
setup_salts() {
    echo "Setting up salts"
    if [ -n "${WORDPRESS_SALTS:-}" ]; then
        count=$(printf '%s\n' "$WORDPRESS_SALTS" | grep -c "define(") || true
        if [ "$count" -ne 8 ]; then
            echo "Error: WORDPRESS_SALTS must contain exactly 8 define(...) lines (got $count)." >&2
            exit 1
        fi
        {
            echo "<?php"
            printf '%s\n' "$WORDPRESS_SALTS"
        } > /var/www/html/wp-salts.php
    else
        echo "WORDPRESS_SALTS not set; generating local salts (fine for dev; set the secret in production)"
        php -r '
            $keys = array("AUTH_KEY","SECURE_AUTH_KEY","LOGGED_IN_KEY","NONCE_KEY","AUTH_SALT","SECURE_AUTH_SALT","LOGGED_IN_SALT","NONCE_SALT");
            $out = "<?php\n";
            foreach ($keys as $k) {
                $out .= sprintf("define(%s%s%s, %s%s%s);\n", chr(39), $k, chr(39), chr(39), bin2hex(random_bytes(32)), chr(39));
            }
            file_put_contents("/var/www/html/wp-salts.php", $out);
        '
    fi
    chmod 640 /var/www/html/wp-salts.php 2>/dev/null || true
}

# Always copy the custom wp-config.php
echo "Copying custom wp-config.php"
cp /wp-config.php /var/www/html/wp-config.php
chmod 644 /var/www/html/wp-config.php

# Check if WordPress is installed
if [ ! -f /var/www/html/wp-includes/version.php ]; then
    install_wordpress
fi

# Check if salts are needed
if [ ! -f /var/www/html/wp-salts.php ]; then
    setup_salts
fi

# Refresh the platform mu-plugins (platform-*.php) without disturbing the
# site's own mu-plugins (which arrive baked in wp-content from git).
echo "Installing platform mu-plugins"
mkdir -p /var/www/html/wp-content/mu-plugins
rm -f /var/www/html/wp-content/mu-plugins/platform-*.php
cp /mu-plugins/platform-*.php /var/www/html/wp-content/mu-plugins/
chmod 755 /var/www/html/wp-content/mu-plugins

# Install the APCu persistent object cache drop-in, unless disabled. It
# self-disables at runtime if APCu is unavailable.
if [ -f /object-cache.php ] && [ "${WORDPRESS_OBJECT_CACHE:-apcu}" != "none" ]; then
    echo "Installing the APCu object cache drop-in (wp-content/object-cache.php)"
    cp /object-cache.php /var/www/html/wp-content/object-cache.php
    chmod 644 /var/www/html/wp-content/object-cache.php
fi

# Install the SQLite Database Integration plugin and the Cloudflare D1
# database drop-in when the image bundles them and a proxy is configured.
if [ -d /wordpress-plugins/sqlite-database-integration ]; then
    echo "Installing the SQLite Database Integration plugin"
    rm -rf /var/www/html/wp-content/plugins/sqlite-database-integration
    mkdir -p /var/www/html/wp-content/plugins
    cp -a /wordpress-plugins/sqlite-database-integration /var/www/html/wp-content/plugins/

    if [ -n "${WP_D1_PROXY_URL:-}" ]; then
        echo "Installing the Cloudflare D1 database drop-in (wp-content/db.php)"
        cp /var/www/html/wp-content/plugins/sqlite-database-integration/wp-includes/database/d1/db.copy \
            /var/www/html/wp-content/db.php
        chmod 644 /var/www/html/wp-content/db.php
    fi
fi

# Execute the main command
exec "$@"