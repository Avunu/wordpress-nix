"""``wordpress-init``: the scaffolder behind ``nix run github:Avunu/wordpress``.

It writes a new Bedrock site, converts a pulled classic WordPress install (or a v2
wordpress-nix site) in place, reconciles a site that already is one, and hands
``--sync``/``--check`` to ``wordpress-nix sync``. The mode comes from the target
directory (detect.py) unless a flag forces one. It mirrors frappe-init: the same
flags, exit codes, plan-then-pipeline shape, backup directory and staging rules.

Modules, in the order a run reaches them: args (the command line), config and
presets (what Nix baked), state (one run), detect (read-only probes), wporg (the
catalog: wordpress.org, WP Packages, Packagist), classify (dispositions and flags),
plan, template, gitignore, composer_json and secret_files (the steps), pipeline (what
the modes share), scaffold and migrate (the modes), command (dispatch).
"""
