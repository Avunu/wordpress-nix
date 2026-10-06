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
  # Build opcache with JIT support. nixpkgs disables it for every ZTS build
  # before PHP 8.5 (see jitPhp below), which silently makes conf/php.ini's
  # `opcache.jit` directive inert -- and FrankenPHP requires ZTS, so that is
  # every build here.
  jit ? true,
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

  # nixpkgs builds the opcache extension with --disable-opcache-jit whenever
  # ztsSupport is set (pkgs/top-level/php-packages.nix:827). FrankenPHP needs
  # ZTS, so conf/php.ini's `opcache.jit = tracing` has never taken effect here:
  # the directive does not exist at runtime and ini_get() returns false.
  #
  # Dropping the flag is worth -21% TTFB and +21% requests per vCPU on a real
  # WordPress page, for ~0.16 s of cold start and ~20 MB of RSS.
  # Measured with the project's own benchmark harness (not published).
  #
  # On PHP 8.5 opcache is built into the interpreter: there is no opcache
  # extension attribute to override (the override would throw) and no JIT
  # gate to remove, so the interpreter is used as is.
  jitPhp =
    if jit && lib.versionOlder php.version "8.5" then
      php.override {
        packageOverrides = _final: prev: {
          extensions = prev.extensions // {
            opcache = prev.extensions.opcache.overrideAttrs (_: { configureFlags = [ ]; });
          };
        };
      }
    else
      php;

  basePhp = jitPhp.override {
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

      # Recommended for caching. opcache is an extension up to PHP 8.4 and part
      # of the interpreter from 8.5, where `all` no longer has it.
      apcu

      # Optional extensions for improved functionality
      gd
      iconv
      sodium

      # Development extensions (uncomment if needed in production)
      # xdebug
    ])
    ++ lib.optionals (lib.versionOlder php.version "8.5") [
      all.opcache
      # igbinary has no PHP 8.5 release yet (nixpkgs marks it broken there);
      # APCu falls back to PHP's own serializer.
      all.igbinary
    ]
    ++ extraExtensions all
  );
in
phpWithExtensions.buildEnv {
  extraConfig = builtins.readFile ../conf/php.ini + "\n" + iniExtra;
}
