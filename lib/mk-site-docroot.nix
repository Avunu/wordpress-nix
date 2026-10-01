# An immutable WordPress document root in the Nix store: the pinned platform
# core, the site's wp-content, and the platform mu-plugins, composed at build
# time.
#
# This is what the read-only public plane of a split-plane site serves. Pass it
# as `services.wordpress-nix.source.path` with `source.type = "git"`; the module
# then builds a symlink farm into this tree and keeps only uploads, cache and
# upgrade as real directories.
#
# Composing mu-plugins in here rather than leaving them to the module is
# deliberate: the module only copies `muPlugins` in state and managed modes,
# because git mode's whole premise is that the tree is already complete. So the
# tree has to arrive complete.
#
#   mkSiteDocroot {
#     inherit pkgs;
#     wpContent = "${siteRepo}/wp-content";
#   }
{
  pkgs,
  wpContent ? null,
  muPlugins ? [ ../mu-plugins ],
  plugins ? { },
  themes ? { },
  wordpressVersion ? null,
  wordpressHash ? null,
  # An alternative core document root, in place of the pinned download. Exists
  # for the checks, which must build without reaching wordpress.org.
  core ? null,
  name ? "wordpress-docroot",
}:
let
  inherit (pkgs) lib;

  coreTree =
    if core != null then
      core
    else
      import ./wordpress-core.nix {
        inherit pkgs;
        version = wordpressVersion;
        hash = wordpressHash;
      };

  copyInto =
    dir: set:
    lib.concatStringsSep "\n" (
      lib.mapAttrsToList (entry: src: ''
        mkdir -p "$out/wp-content/${dir}"
        cp -aL --no-preserve=mode ${src} "$out/wp-content/${dir}/${entry}"
      '') set
    );
in
pkgs.runCommandLocal name
  {
    passthru = {
      core = coreTree;
      wordpressVersion = coreTree.version or null;
    };
    meta.description = "Immutable WordPress document root (pinned core + site wp-content + platform mu-plugins)";
  }
  ''
    mkdir -p "$out"
    cp -rL ${coreTree}/. "$out/"
    chmod -R u+w "$out"

    # The module generates wp-config.php into the writable docroot and symlinks
    # everything else; a copy or a sample in here would only shadow it.
    rm -f "$out/wp-config.php" "$out/wp-config-sample.php"

    ${lib.optionalString (wpContent != null) ''
      # The site's payload overlays core's wp-content rather than replacing it,
      # so core's default themes stay as a floor and a site repo that omits its
      # active theme still renders instead of white-screening.
      cp -aL --no-preserve=mode ${wpContent}/. "$out/wp-content/"
    ''}

    ${copyInto "plugins" plugins}
    ${copyInto "themes" themes}

    # Nothing writable belongs in a store tree. The module creates all three as
    # real directories in the docroot, and in a split-plane site uploads is a
    # shared mount that a store copy would shadow.
    rm -rf "$out/wp-content/uploads" "$out/wp-content/cache" "$out/wp-content/upgrade"

    # A stale database drop-in is the one file here that would be loaded in
    # preference to the module's own choice of backend, and gitium's gitignore
    # means a site repo should never carry one in the first place.
    rm -f "$out/wp-content/db.php"

    # Platform mu-plugins last, replacing any copy the site committed.
    mkdir -p "$out/wp-content/mu-plugins"
    rm -f "$out/wp-content/mu-plugins"/platform-*.php
    ${lib.concatMapStringsSep "\n" (p: ''
      cp -aL --no-preserve=mode ${p}/. "$out/wp-content/mu-plugins/"
    '') muPlugins}
  ''
