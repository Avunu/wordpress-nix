# templates/bedrock-site and the build fixture carry stock Bedrock files
# unedited (v3 plan §4.1: config/application.php, config/environments/*,
# web/wp-config.php, web/index.php and the rest are never edited). Both are
# held to the pinned `bedrock` input, so a Dependabot bump of that input fails
# here until the copies are refreshed from the new tag:
#
#   cp -r <bedrock>/{config,web,wp-cli.yml,.env.example} templates/bedrock-site/
#
# Each copy's .gitignore starts with Bedrock's, followed by wordpress-nix's
# additions.
{
  pkgs,
  checks,
  bedrock,
}:
let
  stock = [
    "config/application.php"
    "config/environments/development.php"
    "config/environments/staging.php"
    "web/index.php"
    "web/wp-config.php"
    "web/app/mu-plugins/bedrock-autoloader.php"
    "web/app/plugins/.gitkeep"
    "web/app/themes/.gitkeep"
    "web/app/uploads/.gitkeep"
    "wp-cli.yml"
    ".env.example"
  ];
in
checks.script "bedrock-template" { tools = [ pkgs.diffutils ]; } ''
  status=0
  for copy in ${../templates/bedrock-site} ${./fixtures/bedrock-site}; do
    for f in ${pkgs.lib.escapeShellArgs stock}; do
      if ! cmp -s ${bedrock}/"$f" "$copy/$f"; then
        echo "$copy/$f differs from Bedrock's ${bedrock}/$f" >&2
        diff -u ${bedrock}/"$f" "$copy/$f" >&2 || true
        status=1
      fi
    done
    lines=$(wc -l < ${bedrock}/.gitignore)
    if ! head -n "$lines" "$copy/.gitignore" | cmp -s - ${bedrock}/.gitignore; then
      echo "$copy/.gitignore does not start with Bedrock's" >&2
      status=1
    fi
  done
  [ "$status" = 0 ]
''
