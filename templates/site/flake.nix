{
  description = "A wordpress-nix site: wp-content payload + identity; the platform comes from wordpress-nix";

  inputs = {
    wordpress-nix.url = "github:Avunu/wordpress";
    # flake-parts resolves perSystem `pkgs` from an input named `nixpkgs`.
    nixpkgs.follows = "wordpress-nix/nixpkgs";
  };

  nixConfig = {
    extra-substituters = [ "https://devenv.cachix.org" ];
    extra-trusted-public-keys = [
      "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw="
    ];
  };

  # This flake exposes:
  #   devShells.<system>.default   — `nix develop` / `devenv up`: the site on the platform stack
  #   packages.<system>.image      — the site's OCI image (pinned core + this wp-content)
  #   packages.<system>.static-assets, .worker — the Cloudflare edge pieces
  #   nixosModules.default         — the site on a NixOS host (managed source mode)

  outputs =
    { self, wordpress-nix, ... }@inputs:
    wordpress-nix.lib.mkFlake { inherit inputs; } (
      { ... }:
      let
        # The site's non-secret wp-config.php constants, shared by the dev
        # shell and the NixOS module. Secrets come from the host's secret
        # store (NixOS) or the environment (dev), never from this file.
        siteConfig = ''
          define('WP_MEMORY_LIMIT', '512M');
        '';
      in
      {
        imports = [ wordpress-nix.flakeModules.default ];

        systems = [
          "x86_64-linux"
          "aarch64-linux"
          "aarch64-darwin"
        ];

        perSystem = {
          wordpress-nix = {
            enable = true;
            siteName = "changeme-site-slug"; # lowercase, digits, dashes
            siteRoot = ./.;
            # sqlite (no server; `wp-import dump.sql`), turso (local tursodb +
            # embedded replica, as in production) or mysql (MariaDB).
            database.type = "sqlite";
            configExtra = siteConfig;
          };
        };

        flake.nixosModules.default =
          { pkgs, lib, ... }:
          {
            imports = [ wordpress-nix.nixosModules.default ];
            services.wordpress-nix = {
              enable = true;
              php = pkgs.php85;
              source = {
                type = "managed";
                siteRepo.url = "git@github.com:CHANGEME/site-repo.git";
              };
              database = {
                type = "turso";
                turso.embedded = true;
              };
              cron.enable = true;
              configExtra = lib.mkBefore siteConfig;
            };
          };
      }
    );
}
