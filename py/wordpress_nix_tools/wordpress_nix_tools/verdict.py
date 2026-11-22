"""The Dependabot decision table (plan §6.4), as standards-core ``DecisionRule``s.

Composer groups (templates/bedrock-site/.github/dependabot.yml): ``wordpress-core``
(roots/wordpress*), ``wp-plugins`` (wp-plugin/*, wp-theme/*; minor and patch),
``avunu`` (avunu/*), ``php-libs`` (the rest; minor and patch).

Every Composer PR is relocked first (``relock``), whatever its update type: a
major left unrelocked would only fail ``ci / flake`` on its stale vendor hash.
relock-push then enables auto-merge only when ``ci.verify_relock`` says ``ok``,
so the composer rows of §6.4 that the update decides live there
(``lockcheck.update_review``), read from the base and head locks:
- ``roots/wordpress*`` patch is auto; minor and major are ``needs-review``;
- any major is ``needs-review``;
- ``wp-plugin/woocommerce*`` minor on a Woo site (the head's
  ``.github/wordpress-nix.json`` ``woocommerce: true``) is ``needs-review``;
- a new package is ``needs-review``; a URL that does not name its package and
  version is ``reject``.
A ``security``-labelled PR bypasses the cooldown in Dependabot's config; its
patch or minor then merges by the rows above.
"""

from standards_core.ci.verdict import DecisionRule

DECISION_TABLE = (
    # github_actions: wordpress-nix's own workflows move only through its rollout.
    DecisionRule("github_actions", "close", names=("Avunu/wordpress*", "$engine"), sticky=True),
    DecisionRule("github_actions", "auto", update="minor-patch"),
    DecisionRule("github_actions", "auto", ungrouped_named=True),
    # composer: every PR is relocked; verify-relock gives the verdict after it.
    DecisionRule("composer", "relock"),
    # nix: a site has no nix ecosystem (wordpress-nix moves by rollout); refuse any.
    DecisionRule("nix", "close", names=("$rollout-inputs",)),
    DecisionRule("nix", "relock"),
)
