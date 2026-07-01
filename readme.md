# WordPress FrankenPHP for Nix

This project provides two ways to deploy an optimized [FrankenPHP](https://frankenphp.dev/)
WordPress stack from a single, shared Nix codebase:

1. **OCI containers** — `nix build .#wordpress-php83` produces an image for ghcr.io etc.
2. **A NixOS module** — `services.wordpress-nix` runs WordPress directly on a NixOS host.

Both paths share the same optimized ZTS PHP build (`lib/php.nix`) and FrankenPHP
(`lib/frankenphp.nix`).

## Features

* PHP 8.2 / 8.3 / 8.4, selectable per deployment (`services.wordpress-nix.php`).
* [FrankenPHP](https://frankenphp.dev/) as the server; standalone Caddy ACME TLS on NixOS.
* Optimized builds with CPU-specific flags (auto-gated per architecture; opt-out via `phpOptimize`).
* Two site-source modes on NixOS:
  * **state** — mutable WordPress core in local state storage (flexible, server-specific;
    admin manages core/plugins/themes via the UI).
  * **git** — a read-only document root pulled from a flake input (source-managed).
* Automated container builds and pushes to ghcr.io.

## Repo layout

```
flake.nix                 # outputs: nixosModules.default, lib, packages, checks
lib/{php,frankenphp,wordpress}.nix   # shared builders
modules/nixos.nix         # services.wordpress-nix
modules/containers.nix    # OCI image build (reuses lib/)
conf/{php.ini,Caddyfile,wp-config.php}
tests/module.nix          # NixOS VM test
```

## NixOS module

Add this flake as an input and import `nixosModules.default`.

### State-managed site (flexible, server-specific)

```nix
{
  imports = [ inputs.wordpress-nix.nixosModules.default ];

  services.wordpress-nix = {
    enable = true;
    php = pkgs.php84;              # per-deployment PHP version
    domain = "blog.example.com";  # Caddy gets a cert via ACME
    acmeEmail = "admin@example.com";
    source.type = "state";        # core lives in /var/lib/wordpress/www, mutable
    database.createLocally = true; # local MariaDB, passwordless unix_socket auth
  };
}
```

### Source-managed site (git)

The WordPress document root (a full webroot, or a Bedrock/Composer layout) is a flake
input; it is mounted read-only, with `wp-content/uploads` kept writable in state.

```nix
{
  # flake inputs:  mysite.url = "git+ssh://git@host/mysite";
  imports = [ inputs.wordpress-nix.nixosModules.default ];

  services.wordpress-nix = {
    enable = true;
    php = pkgs.php83;
    domain = "shop.example.com";
    acmeEmail = "admin@example.com";
    source = {
      type = "git";
      path = inputs.mysite;   # read-only document root
      # manageWpConfig = false;  # set if the repo ships its own wp-config.php
    };
    database = {
      createLocally = false;                 # external DB
      host = "db.internal";
      name = "shop";
      user = "shop";
      passwordFile = config.age.secrets.wp-db.path;  # injected via systemd LoadCredential
    };
  };
}
```

Notes:
* Leaving `domain = ""` binds `:80` only (put your own TLS in front).
* An external DB connects over TCP using `passwordFile`; a local DB uses passwordless
  `unix_socket` auth, so `database.user` must equal `user`.
* Secrets (DB password + salts) are written to `/var/lib/wordpress/wp-secrets.php`
  (0600) at activation and never enter the Nix store.
* Run wp-cli as the service user: `sudo -u wordpress wp ...`.
* In git mode UI-driven plugin/theme installs are disabled (`DISALLOW_FILE_MODS`) —
  manage them in the source.

## Containers

The container path is unchanged: WordPress is downloaded at container start
(`WORDPRESS_SOURCE_URL`, default `wordpress.org/latest.zip`) and configured from
environment variables (see `.env.example` and `conf/wp-config.php`).

### Prerequisites

* Nix with flakes enabled
* Docker (for local testing and pushing)
* GitHub account (for pushing to ghcr.io)

## Local Development

### Building Images

To build images locally:

```bash
# Build all images
nix build

# Build a specific PHP version
nix build .#wordpress-php83
```

### Testing Locally

There’s a script you can use to test locally:

```bash
./scripts/test.sh
```

Visit `http://localhost:8080` in your browser to test.

### Pushing to ghcr.io



1. Create a `.env` file in the project root:

```
GITHUB_USERNAME=your_github_username
GITHUB_TOKEN=your_personal_access_token
```


2\. Run the build and push script:

```bash
./scripts/build-and-push.sh
```

## GitHub Actions

The included GitHub Actions workflow automatically builds and pushes images to ghcr.io on pushes to the main branch.

## Contributing

Contributions are welcome! Please submit pull requests with any improvements or bug fixes.

## License

MIT