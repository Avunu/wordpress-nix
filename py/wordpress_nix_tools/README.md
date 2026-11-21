# wordpress-nix-tools

wordpress-nix's Python tools, on python-devops-nix's standards-core:

- `wordpress-init` (`python -m wordpress_nix_tools.wordpress_init`): the scaffolder
  behind `nix run github:Avunu/wordpress`. It creates a Bedrock site, or converts a
  pulled classic WordPress install in place (`--migrate`).
- `wordpress-nix`: the conversion commands (`legacy`, `db`, `media`, `edge`, `site`,
  `repo adopt`, `cutover`, `audit`) and the standards-core commands (`ci`, `repo`,
  `sync`, ...) with wordpress-nix's product: the Dependabot decision table, the relock
  patch paths and the composer.lock verify-relock check.

See docs/reference/scaffolder.md and docs/reference/wordpress-nix-cli.md in the
repository.
