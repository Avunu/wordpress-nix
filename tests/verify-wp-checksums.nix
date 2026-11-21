# verify-wp-checksums against the bedrock-build fixture's package, with a
# local HTTP server standing in for api.wordpress.org and
# downloads.wordpress.org. The "published" checksums are computed from the
# fixture's artifact zips, as wordpress.org computes them from its own; the
# theme is checked against its locked zip, as in production.
#
#   pristine          passes;
#   a tampered plugin PHP file, an extra PHP file in core, a plugin version
#   wordpress.org does not know: each fails;
#   a changed readme: a warning only.
{
  pkgs,
  checks,
  site,
  verifier,
}:
checks.script "verify-wp-checksums"
  {
    tools = [
      verifier
      pkgs.python3
    ];
  }
  ''
    fail() { echo "FAIL: $*" >&2; exit 1; }
    artifacts=${../tests/fixtures/bedrock-site/artifacts}

    # What wordpress.org would publish for the fixture's core and plugin.
    mkdir -p srv/core/checksums/1.0 srv/plugin-checksums/fixture-plugin
    python3 - "$artifacts" <<'PY'
    import hashlib, json, sys, zipfile
    a = sys.argv[1]
    with zipfile.ZipFile(f"{a}/roots--wordpress-no-content-7.1.5.zip") as z:
        core = {n: hashlib.md5(z.read(n)).hexdigest() for n in z.namelist() if not n.endswith("/") and n != "composer.json"}
    # Real core checksums list wp-content too, which a no-content install lacks.
    core["wp-content/index.php"] = "0" * 32
    json.dump({"checksums": core}, open("srv/core/checksums/1.0/index.html", "w"))
    with zipfile.ZipFile(f"{a}/wp-plugin--fixture-plugin-1.2.0.zip") as z:
        files = {n: {"md5": hashlib.md5(z.read(n)).hexdigest(), "sha256": hashlib.sha256(z.read(n)).hexdigest()}
                 for n in z.namelist() if not n.endswith("/") and n != "composer.json"}
    json.dump({"files": files}, open("srv/plugin-checksums/fixture-plugin/1.2.0.json", "w"))
    PY

    python3 -m http.server --bind 127.0.0.1 --directory srv 8931 >server.log 2>&1 &
    for _ in $(seq 50); do
      python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8931/")' 2>/dev/null && break
      sleep 0.1
    done
    export WORDPRESS_NIX_API_BASE=http://127.0.0.1:8931 WORDPRESS_NIX_DOWNLOADS_BASE=http://127.0.0.1:8931

    fresh() { rm -rf tree; mkdir tree; cp -rP --no-preserve=mode ${site}/. tree/; }
    run() { verify-wp-checksums --root tree > report 2>&1 && status=0 || status=$?; cat report; }

    fresh; run
    [ "$status" = 0 ] || fail "the pristine fixture did not verify"
    grep -q '^3 packages checked, 0 failures' report || fail "expected core, one plugin and one theme"

    fresh; echo '// injected' >> tree/web/app/plugins/fixture-plugin/includes/helper.php; run
    [ "$status" = 1 ] || fail "a tampered plugin file passed"
    grep -q 'FAIL: plugin fixture-plugin 1.2.0: includes/helper.php: differs' report || fail "the tampered file was not named"

    fresh; echo '<?php eval($_POST["x"]);' > tree/web/wp/wp-includes/class-wp-cache.php; run
    [ "$status" = 1 ] || fail "an extra core PHP file passed"
    grep -q 'FAIL: core 7.1.5: wp-includes/class-wp-cache.php: not in the published files' report || fail "the extra file was not named"

    fresh; echo '<?php' > tree/web/app/themes/fixture-theme/404.php; run
    [ "$status" = 1 ] || fail "an extra theme PHP file passed"

    fresh; echo 'edited' >> tree/web/app/plugins/fixture-plugin/readme.txt; run
    [ "$status" = 0 ] || fail "a changed readme failed the run"
    grep -q 'warning: plugin fixture-plugin 1.2.0: readme.txt: differs' report || fail "the readme was not warned about"

    fresh; sed -i 's/"version": "1.2.0"/"version": "9.9.9"/' tree/composer.lock; run
    [ "$status" = 1 ] || fail "a plugin version without published checksums passed"

    echo "verify-wp-checksums: ok"
  ''
