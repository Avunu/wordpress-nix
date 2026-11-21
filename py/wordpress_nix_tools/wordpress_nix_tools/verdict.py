"""The Dependabot decision table (plan §6.4), as standards-core ``DecisionRule``s.

Composer groups (templates/bedrock-site/.github/dependabot.yml): ``wordpress-core``
(roots/wordpress*), ``wp-plugins`` (wp-plugin/*, wp-theme/*; minor and patch),
``avunu`` (avunu/*), ``php-libs`` (the rest; minor and patch). A Composer PR is
always relocked first (``relock``): relock-push enables auto-merge only after
verify-relock says ``ok``, and the auto-merge job applies the rows below.

What the table cannot say in rules, the site's CI says in steps:
- core patch versus minor: the ``wordpress-core`` group carries no update-type
  filter, so ``ci.core_update`` (below) splits it: a patch is ``auto``, a minor
  ``review`` (until previews), a major ``review``;
- Woo minors on a Woo site (``.github/wordpress-nix.json`` ``woocommerce: true``)
  are ``review``: ``woo_review`` below;
- a ``security``-labelled PR bypasses the cooldown in Dependabot's config, not here.
"""

from standards_core.ci.verdict import DecisionRule

DECISION_TABLE = (
    # github_actions: wordpress-nix's own workflows move only through its rollout.
    DecisionRule("github_actions", "close", names=("Avunu/wordpress*", "$engine"), sticky=True),
    DecisionRule("github_actions", "auto", update="minor-patch"),
    DecisionRule("github_actions", "auto", ungrouped_named=True),
    # composer: every PR is relocked; the verdict after the relock is the CI's.
    DecisionRule("composer", "relock"),
    # nix: a site has no nix ecosystem (wordpress-nix moves by rollout); refuse any.
    DecisionRule("nix", "close", names=("$rollout-inputs",)),
    DecisionRule("nix", "relock"),
)

# After the relock (relock-push's `decision` ok): the composer rows of §6.4.
AUTO, REVIEW = "auto", "review"


def composer_after_relock(*, group: str, update_type: str, names: list[str], woocommerce: bool, security: bool) -> str:
    """The §6.4 verdict for a relocked Composer PR whose verify-relock said ok."""
    major = update_type == "version-update:semver-major"
    patch = update_type == "version-update:semver-patch"
    lowered = [n.lower() for n in names]
    if major:
        return REVIEW
    if group == "wordpress-core" or any(n.startswith("roots/wordpress") for n in lowered):
        return AUTO if patch else REVIEW
    if woocommerce and any(n.startswith("wp-plugin/woocommerce") for n in lowered) and not patch:
        return REVIEW
    if group in ("wp-plugins", "avunu", "php-libs") or any(n.startswith("avunu/") for n in lowered):
        return AUTO
    if security and update_type in ("version-update:semver-minor", "version-update:semver-patch"):
        return AUTO
    return REVIEW
