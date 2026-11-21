# wordpress-nix-tools

wordpress-nix's Python tools, on python-devops-nix's standards-core:

- `wordpress-init` (`python -m wordpress_nix_tools.wordpress_init`): the scaffolder
  behind `nix run github:Avunu/wordpress`. It creates a Bedrock site, or converts a
  pulled classic WordPress install in place (`--migrate`).
- `wordpress-nix`: the conversion commands (`legacy`, `db`, `media`, `edge`, `site`,
  `repo adopt`, `cutover`, `audit`) and the standards-core commands (`ci`, `repo`,
  `sync`, ...) with wordpress-nix's product (`wordpress_nix_tools/product.py`):
  - `wordpress-init --sync` / `wordpress-nix sync`: a site's managed files
    (`.github/dependabot.yml`, the `ci`, `deps` and `audit` callers,
    `.github/wordpress-nix.json`, `.editorconfig`, `.pre-commit-config.yaml`,
    `committed.toml`, the `.gitignore` block) under the built-in `deployment@1.0`
    profile, from `wordpress_nix_tools/standards/`;
  - `wordpress-nix ci <step>`: the site workflows' steps, with the Dependabot decision
    table (`verdict.py`), the relock patch path (`nix/vendor-hash`) and `ci
    verify-relock` (`lockcheck.py`: the URL names the same package and version, and
    the §6.4 rows the version change decides);
  - `wordpress-nix audit`: composer.lock and the committed plugins against Wordfence
    Intelligence (`audit.py`).

See docs/reference/scaffolder.md and docs/reference/wordpress-nix-cli.md in the
repository.
