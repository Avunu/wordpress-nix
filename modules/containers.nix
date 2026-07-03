# WordPress FrankenPHP OCI image.
#
# Behaviour-preserving move of the original ./wordpress.nix: same contents,
# entrypoint and layout, but the PHP + FrankenPHP builds now come from the shared
# lib/ so the container and the NixOS module stay in lock-step.
#
#   import ./modules/containers.nix { inherit pkgs; php = pkgs.php83; imageName = "wordpress-php83"; }
{
  pkgs,
  php,
  imageName,
  # Source of the SQLite Database Integration project (with the D1 backend).
  # When set, the image bundles the plugin, the D1 db.php drop-in, and the
  # native wp_mysql_parser + wp_d1_client extensions.
  d1DriverSrc ? null,
  # Package set providing the Rust toolchain for the native extensions.
  rustPkgs ? pkgs,
}:
let
  phpBuild = import ../lib/php.nix { inherit pkgs php; };

  phpExtensions =
    if d1DriverSrc == null then
      null
    else
      import ../lib/php-extensions.nix {
        inherit pkgs rustPkgs;
        php = phpBuild;
        src = d1DriverSrc;
      };

  # The PHP ini scan path: the buildEnv's own configuration, plus the native
  # extensions when enabled. FrankenPHP's embedded PHP does not inherit the
  # CLI wrapper's compiled-in scan directory, so it is set explicitly.
  phpIniScanDir = pkgs.lib.concatStringsSep ":" (
    [ "${phpBuild}/lib" ] ++ pkgs.lib.optional (phpExtensions != null) "${phpExtensions.iniDir}"
  );

  wp-cli = pkgs.wp-cli.override {
    php = phpBuild;
  };

  frankenphp = import ../lib/frankenphp.nix {
    inherit pkgs;
    php = phpBuild;
  };

  caddyfile = pkgs.writeText "Caddyfile" (builtins.readFile ../conf/Caddyfile);

  docker-entrypoint = pkgs.writeScriptBin "docker-entrypoint" (builtins.readFile ../docker-entrypoint.sh);
in
pkgs.dockerTools.buildLayeredImage {
  name = imageName;
  tag = "latest";
  contents = [
    phpBuild
    pkgs.busybox
    pkgs.cacert
    # pkgs.ghostscript
    # pkgs.imagemagick
    pkgs.mariadb.client
    # pkgs.vips
    pkgs.zip
    wp-cli
  ];

  config = {
    Env = [
      "PHP_INI_SCAN_DIR=${phpIniScanDir}"
    ];
    Entrypoint = [
      "${pkgs.busybox}/bin/sh"
      "${pkgs.lib.getExe docker-entrypoint}"
    ];
    Cmd = [
      "${pkgs.lib.getExe frankenphp}"
      "run"
      "--config"
      "${caddyfile}"
      "--adapter"
      "caddyfile"
    ];
    ExposedPorts = {
      "80/tcp" = { };
    };
  };

  extraCommands = ''
    # set up /tmp
    mkdir -p tmp
    chmod 1777 tmp

    # Copy WordPress files
    mkdir -p var/www/html
    cp ${../conf/wp-config.php} wp-config.php

    # The APCu persistent object cache drop-in. The entrypoint installs it
    # as wp-content/object-cache.php unless WORDPRESS_OBJECT_CACHE=none.
    cp ${../conf/object-cache.php} object-cache.php

    # copy must-use plugins
    mkdir mu-plugins
    cp -r ${../mu-plugins}/. mu-plugins/
${
  pkgs.lib.optionalString (d1DriverSrc != null) ''
    # Bundle the SQLite Database Integration plugin with the D1 backend.
    # The entrypoint installs it into the docroot when WP_D1_PROXY_URL is set.
    # -L dereferences the plugin's wp-includes/database symlink.
    mkdir -p wordpress-plugins
    cp -rL ${d1DriverSrc}/packages/plugin-sqlite-database-integration wordpress-plugins/sqlite-database-integration
    chmod -R u+w wordpress-plugins
''
}

    # Symlink CA certificates
    ln -s ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt

    # Symlink busybox for bash and env (required by wp-cli)
    mkdir -p usr/bin
    ln -s ${pkgs.busybox}/bin/busybox usr/bin/bash
    ln -s ${pkgs.busybox}/bin/busybox usr/bin/env
  '';
}
