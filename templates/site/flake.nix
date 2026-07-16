{
  description = "A WordPress-on-Cloudflare site (payload + identity; all platform code comes from wordpress-nix)";

  inputs.wordpress-nix.url = "github:Avunu/wordpress";

  outputs =
    { self, wordpress-nix }:
    let
      # Cloudflare Containers run linux/amd64.
      system = "x86_64-linux";
      pkgs = wordpress-nix.inputs.nixpkgs.legacyPackages.${system};
      wpContent = ./wp-content;
    in
    {
      packages.${system} = {
        # The site's container image: pinned core + this repo's wp-content.
        image = wordpress-nix.lib.mkSiteImage {
          inherit pkgs wpContent;
          imageName = "CHANGEME-site-slug";
        };

        # The Worker-Assets static tree (same pin + this wp-content).
        static-assets = wordpress-nix.lib.mkStaticAssets { inherit pkgs wpContent; };

        # The platform edge Worker bundle.
        worker = wordpress-nix.lib.mkSiteWorker { inherit pkgs; };

        default = self.packages.${system}.image;
      };

      devShells.${system}.default = wordpress-nix.devShells.${system}.default;
    };
}
