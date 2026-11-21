import re

from wordpress_nix_tools.db import rewrite_urls


def apply(steps, text):
    for s in steps:
        text = re.sub(s.pattern, s.replacement.replace("\\", "\\\\"), text)
    return text


STEPS = rewrite_urls.build_steps(
    "https://fine.example", "", "https://media.fine.example", ["cwicly", "fonts", "wpo_wcpdf*", "fluentform/temp"]
)


def test_attachments_go_to_media_plain_and_escaped():
    assert (
        apply(STEPS, "https://fine.example/wp-content/uploads/2024/01/a.jpg")
        == "https://media.fine.example/2024/01/a.jpg"
    )
    assert apply(STEPS, "//www.fine.example/wp-content/uploads/a.jpg") == "https://media.fine.example/a.jpg"
    assert (
        apply(STEPS, r"https:\/\/fine.example\/wp-content\/uploads\/2024\/a.jpg")
        == r"https:\/\/media.fine.example\/2024\/a.jpg"
    )


def test_excluded_prefixes_stay_local():
    assert (
        apply(STEPS, "https://fine.example/wp-content/uploads/cwicly/x.css")
        == "https://fine.example/app/uploads/cwicly/x.css"
    )
    assert (
        apply(STEPS, "http://fine.example/wp-content/uploads/wpo_wcpdf_7/a.pdf")
        == "https://fine.example/app/uploads/wpo_wcpdf_7/a.pdf"
    )
    assert (
        apply(STEPS, r"https:\/\/fine.example\/wp-content\/uploads\/fonts\/a.woff2")
        == r"https:\/\/fine.example\/app\/uploads\/fonts\/a.woff2"
    )


def test_other_hosts_untouched():
    for text in (
        "https://other.example/wp-content/uploads/a.jpg",
        "https://fine.example.evil.com/wp-content/uploads/a.jpg",
        "https://media.fine.example/2024/a.jpg",
    ):
        assert apply(STEPS, text) == text


def test_host_change_only_with_to_home():
    assert apply(STEPS, "https://fine.example/about") == "https://fine.example/about"
    staging = rewrite_urls.build_steps(
        "https://fine.example", "https://staging.fine.example", "https://media.fine.example", []
    )
    assert apply(staging, "https://fine.example/about") == "https://staging.fine.example/about"
    assert apply(staging, "https://fine.example.evil.com") == "https://fine.example.evil.com"
    assert apply(staging, r"https:\/\/www.fine.example\/x") == r"https:\/\/staging.fine.example\/x"


def test_run_drives_wp_in_order():
    calls = []

    def wp(argv):
        calls.append(argv)
        return 0, "", ""

    assert rewrite_urls.run(wp, STEPS, log=lambda m: None) == 0
    assert calls[0][:1] == ["search-replace"] and "--regex" in calls[0] and "--all-tables-with-prefix" in calls[0]
    assert ["core", "update-db"] in calls and ["rewrite", "flush"] in calls
