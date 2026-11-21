# audit-mysql-dump: a dump carrying the shape of a real-world
# compromise -- a users-table trigger, a routine, a view, a DEFINER, and an
# administrator whose capability was written in SQL rather than by WordPress --
# must be refused and reported account by account, and then strip into a dump
# that audits clean with its content untouched. The §8.4 cases (fixture-v3-dump.sql):
# an event, account statements, application passwords, session tokens and MainWP
# options stripped tuple by tuple; obfuscated options, scripts, snippets, a
# home mismatch and an unconfirmed administrator reported until --accept.
{
  pkgs,
  auditor,
}:
pkgs.runCommand "audit-mysql-dump-test"
  {
    nativeBuildInputs = [
      auditor
      pkgs.python3
    ];
    fixture = ./fixture-compromised-dump.sql;
    fixtureV3 = ./fixture-v3-dump.sql;
    surveyV3 = ./survey-v3.json;
  }
  ''
    set -euo pipefail
    fail() { echo "FAIL: $1" >&2; exit 1; }
    has() { grep -q "$1" "$2" || fail "$3"; }

    cp "$fixture" dump.sql

    # --- the audit refuses it, and says why ---------------------------------
    if audit-mysql-dump dump.sql > report.txt 2>&1; then
      fail "a dump carrying a trigger, a routine and a view must exit non-zero"
    fi
    cat report.txt

    has 'trigger  after_insert_comment' report.txt "trigger not reported"
    has 'routine  housekeeping'         report.txt "routine not reported by NAME"
    has 'view     wp_a_view'            report.txt "view not reported"
    has 'DEFINER clause'                report.txt "DEFINER clauses not counted"

    # A trigger that writes to the users tables is the finding that matters most.
    has 'PRIVILEGE ESCALATION'          report.txt "a users-table write was not escalated"

    # Two administrators, not three: a subscriber must not be swept up.
    has 'administrator` (2)'            report.txt "expected exactly 2 administrators"
    has 'id=1 .*login=realadmin'        report.txt "the legitimate admin is missing"
    has 'id=99 .*login=mainclient'      report.txt "the backdoor admin is missing"
    has 'id=99.*written directly in SQL' report.txt \
      "the string-serialized capability must be fingerprinted as SQL-written"
    grep -qE '^  id=1 ' report.txt && grep -q 'id=1 .*SQL' report.txt \
      && fail "WordPress' own b:1 serialization must NOT be fingerprinted"
    if grep -qE '^  id=7 ' report.txt; then fail "a subscriber was reported as an administrator"; fi

    # Regression: wp_users is enriched from a table that mysqldump happens to emit
    # AFTER the capabilities, and this fixture puts it BEFORE them. Depending on
    # that order silently produced "login=?" on half of all dumps.
    has 'id=1 .*email=real@example.com' report.txt "the users table was not joined to the accounts"

    # Regression: an escaped quote must not derail the row scanner, or every row
    # after it is lost.
    has 'id=99' report.txt "rows after an escaped quote were lost"

    has 'OPEN     admin-sql-written:99'   report.txt "the SQL-written administrator is not a finding"

    # --- --strip produces a dump that audits clean --------------------------
    # The SQL-written administrator is a finding --strip cannot fix: unaccepted, exit 1.
    if audit-mysql-dump dump.sql --strip -o clean.sql > /dev/null 2>&1; then
      fail "--strip must still exit 1 while a finding is open"
    fi
    audit-mysql-dump dump.sql --strip -o clean.sql --accept admin-sql-written:99 > /dev/null 2>&1 \
      || fail "--strip with every finding accepted must exit 0"
    audit-mysql-dump clean.sql --accept admin-sql-written:99 > clean-report.txt 2>&1 \
      || fail "the stripped dump must audit clean (exit 0)"
    has 'Executable objects: none' clean-report.txt "something executable survived --strip"
    has 'administrator` (2)'       clean-report.txt "--strip must not touch accounts"

    for pattern in 'TRIGGER `' 'PROCEDURE `' 'VIEW `' 'DEFINER='; do
      if grep -qF "$pattern" clean.sql; then fail "clean.sql still contains: $pattern"; fi
    done
    if grep -q '^DELIMITER' clean.sql; then fail "clean.sql still has a DELIMITER block"; fi

    # Content and schema must come through untouched, which is exactly why
    # account removal is left to SQL after the restore instead of done here.
    # Anchored: the stripped trigger's BODY also contains an INSERT INTO wp_users,
    # and counting that would make a correct strip look like data loss.
    for t in wp_users wp_usermeta wp_posts; do
      a=$(grep -c "^INSERT INTO .$t." dump.sql)
      b=$(grep -c "^INSERT INTO .$t." clean.sql)
      [ "$a" = "$b" ] || fail "$t: $a INSERT statements became $b"
    done
    # The rows themselves, not just the statement count.
    for row in "(1,'realadmin'," "(99,'mainclient',"; do
      grep -qF "$row" clean.sql || fail "a wp_users row was lost: $row"
    done
    [ "$(grep -c '^CREATE TABLE' dump.sql)" = "$(grep -c '^CREATE TABLE' clean.sql)" ] \
      || fail "a CREATE TABLE was lost"
    # The value is stored escaped (o\'brien), so match the part either side of it.
    has "brien@example.com" clean.sql "an escaped value was corrupted"

    # --- the json form is machine-readable ----------------------------------
    audit-mysql-dump dump.sql --json > out.json 2> /dev/null || true
    python3 - <<'PY'
    import json
    d = json.load(open("out.json"))
    assert d["triggers"] == ["after_insert_comment"], d["triggers"]
    assert d["routines"] == ["housekeeping"], d["routines"]
    assert d["views"] == ["wp_a_view"], d["views"]
    assert d["escalation"] == ["after_insert_comment"], d["escalation"]
    assert set(d["administrators"]) == {"1", "99"}, d["administrators"]
    assert d["administrators"]["99"]["written_directly_in_sql"] is True
    assert d["administrators"]["1"]["written_directly_in_sql"] is False
    assert d["administrators"]["1"]["login"] == "realadmin", d["administrators"]["1"]
    assert [f["id"] for f in d["findings"]] == ["admin-sql-written:99"], d["findings"]
    print("json ok")
    PY

    # --- a clean dump is accepted silently ----------------------------------
    audit-mysql-dump clean.sql --accept admin-sql-written:99 > /dev/null 2>&1 || fail "a clean dump must exit 0"
    rc=0; audit-mysql-dump clean.sql --no-such-flag > /dev/null 2>&1 || rc=$?
    [ "$rc" = 2 ] || fail "a bad argument must exit 2 (got $rc)"

    # --- the §8.4 cases -----------------------------------------------------
    cp "$fixtureV3" v3.sql
    rc=0; audit-mysql-dump v3.sql --survey "$surveyV3" > v3-report.txt 2>&1 || rc=$?
    cat v3-report.txt
    [ "$rc" = 1 ] || fail "the v3 fixture must exit 1 (got $rc)"
    has 'event    nightly_admin'              v3-report.txt "the event is not reported"
    has 'account  CREATE USER'                v3-report.txt "CREATE USER is not reported"
    has 'account  GRANT ALL'                  v3-report.txt "GRANT is not reported"
    has '1 application-password row'          v3-report.txt "application passwords are not counted"
    has '2 session-token row'                 v3-report.txt "session tokens are not counted"
    has '2 MainWP option'                     v3-report.txt "MainWP options are not counted"
    for id in option-obfuscated:wp_cache_x option-script:widget_custom_html snippet:wpcb_snippets:3 \
              home-mismatch admin-unconfirmed:5 survey-mismatch:events; do
      has "OPEN     $id" v3-report.txt "finding $id is missing"
    done
    if grep -q 'lazy_big' v3-report.txt; then fail "a non-autoloaded option was reported as obfuscated"; fi

    accept="--accept option-obfuscated:wp_cache_x --accept option-script:widget_custom_html \
      --accept snippet:wpcb_snippets:3 --accept home-mismatch --accept admin-unconfirmed:5 \
      --accept survey-mismatch:events"
    rc=0; audit-mysql-dump v3.sql --survey "$surveyV3" $accept > /dev/null 2>&1 || rc=$?
    [ "$rc" = 1 ] || fail "accepted findings alone must not pass while executables remain (got $rc)"
    audit-mysql-dump v3.sql --survey "$surveyV3" --strip -o v3-clean.sql $accept > /dev/null 2>&1 \
      || fail "--strip with every finding accepted must exit 0"
    for pattern in 'CREATE USER' 'GRANT ' 'EVENT `' '_application_passwords' 'session_tokens' 'mainwp_child_' 'DEFINER='; do
      if grep -qF "$pattern" v3-clean.sql; then fail "v3-clean.sql still contains: $pattern"; fi
    done
    # The other tuples of a rewritten statement are byte for byte the originals.
    for row in "(4,'blogname','Keep; me\'s title','yes')" "(13,1,'nickname','o\'admin, (the) first')" \
               "(10,1,'wpx_capabilities','a:1:{s:13:\\\"administrator\\\";b:1;}')"; do
      grep -qF "$row" v3-clean.sql || fail "a kept tuple changed or was lost: $row"
    done
    grep -qF "INSERT INTO \`wpx_wpcb_snippets\`" v3-clean.sql || fail "the snippet table was dropped"
    audit-mysql-dump v3-clean.sql --survey "$surveyV3" $accept > /dev/null 2>&1 \
      || fail "the stripped v3 dump must audit clean with its findings accepted"

    touch $out
  ''
