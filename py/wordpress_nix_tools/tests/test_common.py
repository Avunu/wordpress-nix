import pytest

from wordpress_nix_tools.common import phpserial, versions


@pytest.mark.parametrize(
    "a,b,want",
    [
        ("1.0", "1.0.0", -1),
        ("1.2.3", "1.2.3", 0),
        ("6.8.3", "6.6", 1),
        ("6.7-beta2", "6.7", -1),
        ("6.7-RC1", "6.7-beta3", 1),
        ("v2.0", "2.0", 0),
        ("1.10", "1.9", 1),
    ],
)
def test_compare(a, b, want):
    assert versions.compare(a, b) == want


def test_caret():
    assert versions.caret("5.3.1") == "^5.3"
    assert versions.caret("0.10.0") == "^0.10"
    assert versions.caret("7") == "^7"


def test_unserialize_active_plugins():
    raw = 'a:2:{i:0;s:19:"akismet/akismet.php";i:1;s:9:"hello.php";}'
    assert phpserial.as_list(phpserial.loads(raw)) == ["akismet/akismet.php", "hello.php"]


def test_unserialize_bytes_and_nesting():
    raw = 'a:2:{s:4:"file";s:6:"é.jpg";s:5:"sizes";a:1:{s:5:"thumb";a:1:{s:4:"file";s:5:"t.jpg";}}}'
    v = phpserial.loads(raw)
    assert v["file"] == "é.jpg"
    assert v["sizes"]["thumb"]["file"] == "t.jpg"


def test_unserialize_refuses_objects():
    with pytest.raises(phpserial.PhpSerialError):
        phpserial.loads('O:8:"stdClass":0:{}')


def test_mysql_unescape():
    assert phpserial.mysql_unescape(r"o\'brien\n") == "o'brien\n"
