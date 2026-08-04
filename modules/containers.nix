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
  tag ? "latest",
  # Source of the SQLite Database Integration project (with the D1 backend).
  # When set, the image bundles the plugin, the D1 db.php drop-in, and the
  # native wp_mysql_parser + wp_d1_client extensions.
  d1DriverSrc ? null,
  # Package set providing the Rust toolchain for the native extensions.
  rustPkgs ? pkgs,
  # WordPress core is baked into the image (at /usr/src/wordpress) so the
  # image needs no runtime downloads, and so the exact static-asset set is
  # known (lib/static-assets.nix builds the Worker-Assets tree from the same
  # pin). Defaults live in lib/wordpress-core.nix.
  wordpressVersion ? null,
  wordpressHash ? null,
  # The site's wp-content tree (typically a site repo's ./wp-content),
  # grafted over the baked core's wp-content — the per-site image payload.
  wpContent ? null,
  # Extra nix-pinned plugins/themes grafted on top (name -> store path),
  # for site code deliberately kept out of git (e.g. hash-fetched premium
  # plugins). Day-to-day site code belongs in wpContent.
  plugins ? { },
  themes ? { },
}:
let
  phpBuild = import ../lib/php.nix { inherit pkgs php; };

  wordpressCore = import ../lib/wordpress-core.nix {
    inherit pkgs;
    version = wordpressVersion;
    hash = wordpressHash;
  };

  # Graft name -> store-path sets into the baked wp-content.
  graftInto =
    dir: set:
    pkgs.lib.concatStringsSep "\n" (
      pkgs.lib.mapAttrsToList (name: src: ''
        mkdir -p usr/src/wordpress/wp-content/${dir}
        cp -rL ${src} usr/src/wordpress/wp-content/${dir}/${name}
      '') set
    );

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
  inherit tag;
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

    # Bake WordPress core into the image. The entrypoint installs it into the
    # docroot on first boot (no runtime download).
    mkdir -p usr/src
    cp -r ${wordpressCore} usr/src/wordpress
    chmod -R u+w usr/src/wordpress
${
  pkgs.lib.optionalString (wpContent != null) ''
    # Graft the site's wp-content over the baked core's (merge; site wins).
    cp -rL ${wpContent}/. usr/src/wordpress/wp-content/
    chmod -R u+w usr/src/wordpress/wp-content
''
}
${graftInto "plugins" plugins}
${graftInto "themes" themes}

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
