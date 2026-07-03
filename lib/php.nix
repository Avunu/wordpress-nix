# Optimized, WordPress-ready PHP build (ZTS + FrankenPHP-compatible).
#
# Extracted from the original wordpress.nix so both the OCI container build
# (modules/containers.nix) and the NixOS module (modules/nixos.nix) share one
# PHP builder. Returns a `buildEnv` PHP with the WordPress extension set and
# conf/php.ini applied.
#
#   mkPhp { pkgs; php = pkgs.php84; }
{
  pkgs,
  php ? pkgs.php83,
  # Apply the aggressive clang/LTO/march optimization pass. Disable for
  # portability (e.g. exotic CPUs) or faster/simpler builds.
  optimize ? true,
  # Append extra PHP extensions: `all: [ all.redis ]`.
  extraExtensions ? (_all: [ ]),
  # Extra php.ini lines appended after conf/php.ini (later keys win).
  iniExtra ? "",
}:
let
  inherit (pkgs) lib;

  # -march=x86-64-v3 is x86-only; it breaks the build on aarch64. Gate the
  # micro-arch flag on the host platform and fall back to no arch flag on
  # unknown platforms so the build never hard-fails.
  archFlags =
    if pkgs.stdenv.hostPlatform.isx86_64 then
      "-march=x86-64-v3 -mtune=x86-64-v3"
    else if pkgs.stdenv.hostPlatform.isAarch64 then
      # Fixed baseline (reproducible across build hosts, unlike -mcpu=native).
      "-mcpu=neoverse-n1"
    else
      "";
  optCFlags = "${archFlags} -O3 -ffast-math -flto";

  basePhp = php.override {
    # SAPI flags
    cgiSupport = false;
    cliSupport = true;
    fpmSupport = false;
    pearSupport = false;
    pharSupport = true;
    phpdbgSupport = false;

    # Misc flags
    apxs2Support = false;
    argon2Support = true;
    cgotoSupport = false;
    embedSupport = true;
    ipv6Support = true;
    staticSupport = false;
    systemdSupport = false;
    valgrindSupport = false;
    zendMaxExecutionTimersSupport = true;
    zendSignalsSupport = false;
    ztsSupport = true;
  };

  customPhp = basePhp.overrideAttrs (oldAttrs: {
    # Use Clang instead of GCC
    stdenv = pkgs.clangStdenv;

    # optimizations
    extraConfig = lib.optionalString optimize ''
      CC = "${pkgs.llvmPackages_22.clang}/bin/clang";
      CXX = "${pkgs.llvmPackages_22.clang}/bin/clang++";
      CFLAGS="$CFLAGS ${optCFlags}"
      CXXFLAGS="$CXXFLAGS ${optCFlags}"
      LDFLAGS="$LDFLAGS -flto"
    '';

    # Explicitly enable XML support (required by FrankenPHP)
    configureFlags = (oldAttrs.configureFlags or [ ]) ++ [
      "--enable-xml"
      "--with-libxml"
    ];

    buildInputs = (oldAttrs.buildInputs or [ ]) ++ [
      pkgs.libxml2.dev
    ];
  });

  phpWithExtensions = customPhp.withExtensions (
    { all, ... }:
    (with all; [
      # Required extensions
      mysqli

      # Highly recommended extensions
      ctype
      curl
      dom
      exif
      fileinfo
      filter
      igbinary
      # imagick
      intl
      mbstring
      openssl
      pdo
      pdo_mysql
      session
      simplexml
      tokenizer
      xmlwriter
      zip
      zlib

      # Recommended for caching
      opcache
      apcu

      # Optional extensions for improved functionality
      gd
      iconv
      sodium

      # Development extensions (uncomment if needed in production)
      # xdebug
    ])
    ++ extraExtensions all
  );
in
phpWithExtensions.buildEnv {
  extraConfig = builtins.readFile ../conf/php.ini + "\n" + iniExtra;
}
