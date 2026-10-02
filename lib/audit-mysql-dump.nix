# lib/audit-mysql-dump.nix — the migration's gate on an untrusted dump.
#
# A dump from a host we do not control can carry objects that EXECUTE on the new
# server as soon as they are restored. The dump taken to migrate
# one client site carried a trigger on wp_comments that minted an
# administrator whenever a comment matched a phrase, plus the account it had
# already minted. This refuses that dump, and with --strip produces one that is
# only schema and data.
#
#   nix run .#audit-mysql-dump -- dump.sql
#   nix run .#audit-mysql-dump -- dump.sql --strip -o clean.sql
{ pkgs }:
pkgs.writers.writePython3Bin "audit-mysql-dump"
  {
    libraries = [ ];
    # The scanner deliberately shadows names across branches and keeps the
    # report's column alignment, which flake8 reads as style errors.
    flakeIgnore = [
      "E501"
      "E203"
      "W503"
    ];
  }
  (builtins.readFile ../tools/audit-mysql-dump.py)
