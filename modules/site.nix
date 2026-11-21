# `wordpress-nix.site`: a stock Bedrock site repository's flake outputs, from
# lib.mkBedrockSite. What a v3 site flake sets (templates/bedrock-site):
#
#   outputs = inputs@{ wordpress-nix, ... }:
#     wordpress-nix.lib.mkFlake { inherit inputs; } {
#       imports = [ wordpress-nix.flakeModules.default ];
#       wordpress-nix.site = { name = "mysite"; src = ./.; };
#     };
#
# Per system, it gives:
#   packages.default        the site (lib/mk-bedrock-site.nix)
#   packages.vendor         its vendor FOD
#   apps.relock             rewrite nix/vendor-hash (lib/relock.nix)
#   apps.verify-wp-checksums  the package against wordpress.org (network)
#   checks.composer-validate  `composer validate` as nixpkgs runs it, plus --check-lock
#   checks.vendor-hash      nix/vendor-hash is well-formed and not lib.fakeHash
#   checks.build            the package, with mkBedrockSite's assertions
#
# Every check is pure: nothing in them reaches the network. The audits that
# must (composer audit, verify-wp-checksums) are CI job steps.
localFlake:
{ lib, config, ... }:
let
  inherit (lib) mkOption types;
  cfg = config.wordpress-nix.site;

  siteOptions = {
    options = {
      name = mkOption {
        type = types.strMatching "^[a-z0-9][a-z0-9-]*$";
        description = "The site's slug: the package's name, and the repository's (site-<name>).";
        example = "examplepainting";
      };

      src = mkOption {
        type = types.path;
        description = "The site repository's root: composer.json, composer.lock, config/, web/.";
        example = lib.literalExpression "./.";
      };

      php = mkOption {
        type = types.enum [
          "php83"
          "php84"
          "php85"
        ];
        default = "php84";
        description = ''
          The nixpkgs PHP the site builds with, and the base of the optimized
          PHP it runs on. Keep composer.json's config.platform.php in step.
        '';
      };

      vendorHashFile = mkOption {
        type = types.path;
        default = cfg.src + "/nix/vendor-hash";
        defaultText = lib.literalExpression "src + \"/nix/vendor-hash\"";
        description = "The vendor FOD's hash, one line, written by `nix run .#relock`.";
      };

      productionOnlyPlugins = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Plugin directories active in production only (the platform-environment mu-plugin filters them elsewhere).";
        example = [ "fluent-smtp" ];
      };

      developmentOnlyPlugins = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Plugin directories active outside production only.";
        example = [ "wpcodebox2" ];
      };

      activeTheme = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = "The theme the smoke test activates; null leaves the database's.";
        example = "cwicly";
      };
    };
  };
in
{
  options.wordpress-nix.site = mkOption {
    type = types.nullOr (types.submodule siteOptions);
    default = null;
    description = "A Bedrock site repository's package, apps and checks. Null adds none.";
  };

  config = lib.mkIf (cfg != null) {
    # Production is x86_64-linux; a site that develops elsewhere adds its own.
    systems = lib.mkDefault [ "x86_64-linux" ];

    perSystem =
      { pkgs, system, ... }:
      let
        # A site flake has no nixpkgs input of its own (it arrives through
        # wordpress-nix, and in the cluster through cluster/wordpress-nix), so
        # flake-parts' default, inputs'.nixpkgs, is wordpress-nix's here.
        nixpkgs = localFlake.inputs.nixpkgs;
        site = localFlake.lib.mkBedrockSite {
          inherit pkgs;
          inherit (cfg)
            name
            src
            vendorHashFile
            productionOnlyPlugins
            developmentOnlyPlugins
            activeTheme
            ;
          php = pkgs.${cfg.php};
        };
        checks = localFlake.inputs.python-devops-nix.lib.checks.mk {
          inherit pkgs;
          product = "site-${cfg.name}";
        };
        relock = import ../lib/relock.nix { inherit pkgs; };
        verify = localFlake.packages.${system}.verify-wp-checksums;
        verifyPackage = pkgs.writeShellApplication {
          name = "verify-wp-checksums";
          text = ''exec ${lib.getExe verify} --root ${site} "$@"'';
        };
        vendorHash = lib.fileContents cfg.vendorHashFile;
      in
      {
        _module.args.pkgs = lib.mkDefault nixpkgs.legacyPackages.${system};

        packages = {
          default = site;
          inherit (site) vendor;
        };

        apps = {
          relock = {
            type = "app";
            program = lib.getExe relock;
            meta.description = "Rewrite nix/vendor-hash after a composer.lock change";
          };
          verify-wp-checksums = {
            type = "app";
            program = lib.getExe verifyPackage;
            meta.description = "Compare the built site with wordpress.org's published files (needs the network)";
          };
        };

        checks = {
          # nixpkgs' checkComposerValidate flags, with --check-lock (composer.lock
          # agrees with composer.json) and without the publish-only checks.
          composer-validate =
            checks.script "composer-validate"
              {
                src = lib.fileset.toSource {
                  root = cfg.src;
                  fileset = lib.fileset.unions [
                    (cfg.src + "/composer.json")
                    (cfg.src + "/composer.lock")
                  ];
                };
                tools = [ pkgs.${cfg.php}.packages.composer ];
              }
              ''
                composer validate --strict --no-interaction --no-check-all --no-check-publish --check-lock
              '';

          vendor-hash = checks.eval "vendor-hash" {
            assertion =
              builtins.match "sha256-[A-Za-z0-9+/]{43}=" vendorHash != null && vendorHash != lib.fakeHash;
            message = "nix/vendor-hash must hold one sha256 SRI hash, not lib.fakeHash; run `nix run .#relock`";
          };

          build = site;
        };
      };
  };
}
