from wordpress_nix_tools.media import offload

META = 'a:3:{s:4:"file";s:13:"2024/01/a.jpg";s:14:"original_image";s:9:"a-big.jpg";s:5:"sizes";a:1:{s:9:"thumbnail";a:1:{s:4:"file";s:13:"a-150x150.jpg";}}}'


def test_attachment_paths():
    atts = {"1": {"file": "2024/01/a.jpg", "meta": META}, "2": {"file": "woocommerce_uploads/p.zip"}}
    assert offload.attachment_paths(atts) == {
        "2024/01/a.jpg",
        "2024/01/a-big.jpg",
        "2024/01/a-150x150.jpg",
        "woocommerce_uploads/p.zip",
    }


PREFIXES = ["cwicly", "fonts", "woocommerce_uploads", "wpo_wcpdf*", "fluentform"]


def test_plan_partitions():
    atts = {"2024/01/a.jpg", "2024/01/a-150x150.jpg", "woocommerce_uploads/p.zip", "2024/01/gone.jpg"}
    source = [
        "2024/01/a.jpg",
        "2024/01/a-150x150.jpg",
        "woocommerce_uploads/p.zip",
        "cwicly/css/x.css",
        "wpcodebox/index.php",
        "x.php",
        "2024/01/shell.phtml",
        "unknown/data.json",
    ]
    p = offload.plan(atts, source, prefixes=PREFIXES)
    assert p.offload == ["2024/01/a-150x150.jpg", "2024/01/a.jpg"]
    assert p.missing == ["2024/01/gone.jpg"]
    assert p.excluded_attachments == ["woocommerce_uploads/p.zip"]
    assert sorted(p.local) == [
        "cwicly/css/x.css",
        "unknown/data.json",
        "woocommerce_uploads/p.zip",
        "wpcodebox/index.php",
    ]
    assert sorted(p.executables) == ["2024/01/shell.phtml", "x.php"]
    inv = offload.inventory(p, PREFIXES)
    assert "unknown" in inv["excludePrefixes"] and "cwicly" in inv["excludePrefixes"]
    assert "2024" not in inv["excludePrefixes"]


def test_stub_only_when_exact():
    p = offload.plan(set(), ["wpcodebox/index.php"], prefixes=[], stub_ok=lambda path: False)
    assert p.executables == ["wpcodebox/index.php"] and p.local == []


def test_adopt():
    atts = {"a.jpg", "b.jpg", "c.jpg"}
    p = offload.plan(atts, ["a.jpg", "b.jpg"], prefixes=[], bucket=["b.jpg", "c.jpg"])
    assert p.adopted == ["b.jpg", "c.jpg"]
    assert p.offload == ["a.jpg"]
    assert p.missing == []
    p = offload.plan({"d.jpg"}, [], prefixes=[], bucket=[])
    assert p.missing == ["d.jpg"]


def test_excluded_globs():
    assert offload.excluded("wpo_wcpdf_9/x.pdf", ["wpo_wcpdf*"])
    assert offload.excluded("fluentform/temp/x", ["fluentform/temp"])
    assert not offload.excluded("fluentform/entries/x", ["fluentform/temp"])
