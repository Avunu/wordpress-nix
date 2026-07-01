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
}:
let
  phpBuild = import ../lib/php.nix { inherit pkgs php; };

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
    pkgs.mysql.client
    # pkgs.vips
    pkgs.zip
    wp-cli
  ];

  config = {
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

    # copy must-use plugins
    mkdir mu-plugins
    cp -r ${../mu-plugins}/. mu-plugins/

    # Symlink CA certificates
    ln -s ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt etc/ssl/certs/ca-certificates.crt

    # Symlink busybox for bash and env (required by wp-cli)
    mkdir -p usr/bin
    ln -s ${pkgs.busybox}/bin/busybox usr/bin/bash
    ln -s ${pkgs.busybox}/bin/busybox usr/bin/env
  '';
}
