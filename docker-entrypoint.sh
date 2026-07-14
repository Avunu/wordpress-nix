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

# Default WordPress URL if not provided
WORDPRESS_SOURCE_URL=${WORDPRESS_SOURCE_URL:-"https://wordpress.org/latest.zip"}

# Function to install WordPress: from the core baked into the image when
# present (fast, offline), otherwise downloaded from WORDPRESS_SOURCE_URL.
install_wordpress() {
    if [ -f /usr/src/wordpress/wp-includes/version.php ]; then
        echo "Installing bundled WordPress core from /usr/src/wordpress"
        cp -r /usr/src/wordpress/. /var/www/html/
        chmod -R u+w /var/www/html
    else
        echo "WordPress not found. Downloading and installing from: $WORDPRESS_SOURCE_URL"
        wget -O wordpress.zip "$WORDPRESS_SOURCE_URL"

        # Create a temporary directory for extraction
        TEMP_DIR="/tmp/wordpress"
        mkdir -p "$TEMP_DIR"
        unzip wordpress.zip -d "$TEMP_DIR"

        # Find WordPress files
        WP_ROOT=$(find "$TEMP_DIR" -name wp-config-sample.php -exec dirname {} \; | head -n 1)

        if [ -z "$WP_ROOT" ]; then
            echo "Error: WordPress files not found in the downloaded archive."
            exit 1
        fi

        # Move WordPress files to the correct location
        mv "$WP_ROOT"/* /var/www/html/

        # Clean up
        rm -rf "$TEMP_DIR" wordpress.zip
    fi

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

# function to set up the salts
setup_salts() {
    echo "Setting up salts"
    echo "<?php" > /var/www/html/wp-salts.php
    wget -qO- https://api.wordpress.org/secret-key/1.1/salt/ >> /var/www/html/wp-salts.php
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

# Always copy the custom mu-plugins
echo "Copying custom mu-plugins"
rm -rf /var/www/html/wp-content/mu-plugins
mkdir -p /var/www/html/wp-content/mu-plugins
cp -a /mu-plugins/. /var/www/html/wp-content/mu-plugins/
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