"""survey.json, the record ``legacy survey`` writes and the scaffolder reads.

    {"schema": 1, "host": "...", "path": "...", "takenAt": "...",
     "wordpress": {"version", "multisite", "tablePrefix", "locale", "siteurl", "home"},
     "php": "8.2.30", "mariadb": "10.6.21",
     "plugins": [{"name": "akismet", "file": "akismet/akismet.php", "version": "5.3",
                  "active": true}],
     "muPlugins": [...], "dropins": [...],
     "themes": [{"name": "cwicly", "version": "1.4", "active": true, "parent": ""}],
     "activeTheme": {"stylesheet": "...", "template": "..."},
     "checksums": {"core": "ok" | "failed" | "unavailable", "plugins": {name: status}},
     "administrators": [{"id", "login", "email", "registered"}],
     "uploads": {"bytes", "dirs", "phpFiles"},
     "woocommerce": bool, "dbBytes": N,
     "dbCode": {"triggers": [...], "routines": [...], "events": [...], "views": [...]},
     "xcloud": bool,
     "cloudflare": {"zone", "account", "status", "reachable", "apo", "rules"},
     "score": N}

``plugins[].name`` is the directory (or the file, for a single-file plugin), as the
classifier names items.
"""

from typing import Any

from wordpress_nix_tools.common.phpserial import PhpSerialError, as_list, loads

SCHEMA = 1


def plugin_name(file: str) -> str:
    """``akismet/akismet.php`` → ``akismet``; ``hello.php`` → ``hello.php``."""
    return file.split("/", 1)[0]


def active_plugins(survey: dict[str, Any]) -> set[str]:
    return {p["name"] for p in survey.get("plugins", []) if p.get("active")}


def active_theme(survey: dict[str, Any]) -> tuple[str, str]:
    t = survey.get("activeTheme") or {}
    return t.get("stylesheet", ""), t.get("template", "") or t.get("stylesheet", "")


def active_from_options(options: dict[str, str]) -> tuple[set[str], str, str]:
    """The active set from a dump's options: (plugins, stylesheet, template)."""
    plugins: set[str] = set()
    raw = options.get("active_plugins")
    if raw:
        try:
            plugins = {plugin_name(str(v)) for v in as_list(loads(raw))}
        except PhpSerialError:
            plugins = set()
    return plugins, options.get("stylesheet", ""), options.get("template", "")


def complexity(survey: dict[str, Any], weights: dict[str, int] | None = None) -> int:
    """The fleet-mode complexity score (§8.2): plugins × weights + Woo + premium count
    + writers + PHP gap + locale + db-code + not on Cloudflare."""
    w = {"plugin": 1, "premium": 3, "woo": 15, "writer": 2, "php": 5, "locale": 5, "dbcode": 10, "cf": 20}
    w.update(weights or {})
    plugins = survey.get("plugins", [])
    premium = sum(1 for p in plugins if p.get("premium"))
    writers = sum(1 for p in plugins if p.get("writer"))
    score = w["plugin"] * len(plugins) + w["premium"] * premium + w["writer"] * writers
    if survey.get("woocommerce"):
        score += w["woo"]
    php = str(survey.get("php", ""))
    if php and not php.startswith(("8.3", "8.4", "8.5")):
        score += w["php"]
    if (survey.get("wordpress") or {}).get("locale", "en_US") not in ("en_US", ""):
        score += w["locale"]
    code = survey.get("dbCode") or {}
    if any(code.get(k) for k in ("triggers", "routines", "events", "views")):
        score += w["dbcode"]
    cf = survey.get("cloudflare") or {}
    if cf.get("status") != "active" or cf.get("type", "full") != "full":
        score += w["cf"]
    return score
