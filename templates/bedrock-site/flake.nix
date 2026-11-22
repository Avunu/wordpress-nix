{
  description = "A WordPress site: stock Roots Bedrock, built and served by wordpress-nix";

  # nixpkgs arrives through wordpress-nix (and, in the cluster, through
  # cluster/wordpress-nix); the site declares nothing else.
  inputs.wordpress-nix.url = "github:Avunu/wordpress/v3.0.0";

  # wordpress-nix's cache holds the optimized PHP and FrankenPHP builds, which
  # cache.nixos.org lacks. Nix only honours a flake's own nixConfig.
  nixConfig = {
    extra-substituters = [
      "https://devenv.cachix.org"
      "https://wordpress-nix.cachix.org"
    ];
    extra-trusted-public-keys = [
      "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw="
      "wordpress-nix.cachix.org-1:CYaSELCIUp9U26Ko+ajzE7M+l/dtiG5hBLEtvFXLUeA="
    ];
  };

  # packages.default (the site), packages.vendor, apps.relock,
  # apps.verify-wp-checksums and the pure checks: wordpress-nix's
  # modules/site.nix.
  outputs =
    inputs@{ wordpress-nix, ... }:
    wordpress-nix.lib.mkFlake { inherit inputs; } {
      imports = [ wordpress-nix.flakeModules.default ];
      wordpress-nix.site = {
        name = "changeme"; # the slug: lowercase, digits, dashes
        src = ./.; # vendorHashFile defaults to ./nix/vendor-hash
        php = "php84"; # keep composer.json's config.platform.php in step
        productionOnlyPlugins = [ ];
        developmentOnlyPlugins = [ ];
        activeTheme = "twentytwentyfive";
      };
    };
}
