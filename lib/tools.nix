# wordpress-nix-tools (py/wordpress_nix_tools) and the two programs built on it:
#
#   package        the Python package, on standards-core from python-devops-nix
#                  (its lib.standards.package), with the repository's data/ copied
#                  into it; its pytest suite is the check phase, with the files it
#                  reads from the repository under $WORDPRESS_NIX_REPO.
#   wordpress-init `nix run github:Avunu/wordpress`: the scaffolder, run as
#                  `python -I -m wordpress_nix_tools.wordpress_init` with the store
#                  paths it reads in WORDPRESS_NIX_CLI_CONFIG, as frappe-init is.
#   wordpress-nix  the conversion and standards commands, with ssh, rsync,
#                  rclone, gh, git, age, curl and the dump and checksum tools on
#                  its PATH.
{
  pkgs,
  base, # python-devops-nix's lib
  coreRev ? null,
  auditMysqlDump,
  verifyWpChecksums,
}:
let
  inherit (pkgs) lib;
  python = pkgs.python3;
  src = ../py/wordpress_nix_tools;
  standardsCore = base.standards.package {
    inherit pkgs python;
    rev = coreRev;
  };
  # What the suite reads from the repository, at the repository's paths.
  repoFiles = lib.fileset.toSource {
    root = ./..;
    fileset = lib.fileset.unions [
      ../data
      ../templates/bedrock-site
      ../tools/audit-mysql-dump.py
      ../tools/test/fixture-v3-dump.sql
      ../docs/reference/scaffolder.md
    ];
  };
  composer = pkgs.php84.packages.composer;

  package = python.pkgs.buildPythonPackage {
    pname = "wordpress-nix-tools";
    inherit ((lib.importTOML (src + "/pyproject.toml")).project) version;
    pyproject = true;
    src = lib.fileset.toSource {
      root = src;
      fileset = lib.fileset.difference src (
        lib.fileset.unions [
          (lib.fileset.fileFilter (f: f.hasExt "pyc") src)
          (lib.fileset.maybeMissing (src + "/.pytest_cache"))
        ]
      );
    };
    # The data files ship inside the package (common/data.py looks there first).
    postPatch = ''
      cp -r ${../data} wordpress_nix_tools/data
      chmod -R u+w wordpress_nix_tools/data
    '';
    build-system = [ python.pkgs.flit-core ];
    dependencies = [ standardsCore ];
    nativeCheckInputs = [
      python.pkgs.pytestCheckHook
      pkgs.git
      composer
    ];
    preCheck = ''
      export WORDPRESS_NIX_REPO=${repoFiles}
      export HOME=$TMPDIR
      export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
    '';
    pythonImportsCheck = [
      "wordpress_nix_tools.cli"
      "wordpress_nix_tools.wordpress_init.command"
    ];
  };

  env = python.withPackages (_: [ package ]);

  wordpressNix = pkgs.writeShellApplication {
    name = "wordpress-nix";
    runtimeInputs = with pkgs; [
      git
      gh
      openssh
      rsync
      rclone
      curl
      age
      gnutar
      gzip
      auditMysqlDump
      verifyWpChecksums
    ];
    text = ''
      exec ${env}/bin/python3 -I -m wordpress_nix_tools "$@"
    '';
    meta = {
      description = "wordpress-nix's conversion commands (legacy, db, media, edge, site, cutover) and site CI steps";
      mainProgram = "wordpress-nix";
    };
  };

  # What wordpress-init reads (wordpress_init/config.py); it refuses an unknown key.
  config = pkgs.writeText "wordpress-init.json" (
    builtins.toJSON {
      template = "${../templates/bedrock-site}";
      data = "${../data}";
    }
  );

  wordpressInit = pkgs.writeShellApplication {
    name = "wordpress-init";
    # git and composer for the conversion, wordpress-nix for --sync: appended to
    # the caller's PATH, not prepended, so the caller's own come first. nix is
    # always the caller's: relock and flake lock talk to the caller's daemon.
    text = ''
      export PATH="$PATH:${
        lib.makeBinPath [
          pkgs.git
          composer
          wordpressNix
        ]
      }"
      WORDPRESS_NIX_CLI_CONFIG=${config} exec ${env}/bin/python3 -I -m wordpress_nix_tools.wordpress_init "$@"
    '';
    meta = {
      description = "Scaffold a wordpress-nix site, or convert a pulled classic WordPress install to one";
      mainProgram = "wordpress-init";
    };
  };
in
{
  inherit package wordpressInit wordpressNix;
}
