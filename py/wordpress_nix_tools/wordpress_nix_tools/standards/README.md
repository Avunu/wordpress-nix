# wordpress-nix standards data

standards-core's data root for wordpress-nix (`Product.data_root`): the managed
files of a site repository under the built-in `deployment@1.0` profile (the
`wordpress-site` standards profile).

- `manifest.d/`: which module manages which file;
- `templates/`: the files `wordpress-nix sync` renders (the Dependabot file, the
  `ci`, `deps` and `audit` workflow callers, `.github/wordpress-nix.json`,
  `.editorconfig`, `.pre-commit-config.yaml`, `committed.toml`);
- `profiles/`, `schema/`: the profiles and the `[tool.wordpress-nix]` schema.

The conversion tools need none of them; their tables are the repository's `data/`.
