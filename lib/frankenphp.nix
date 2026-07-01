# FrankenPHP built against a specific (ZTS) PHP build.
#
# Pass the result of lib/php.nix so the embedded PHP matches the extensions and
# php.ini used everywhere else.
#
#   mkFrankenphp { pkgs; php = mkPhp { inherit pkgs; }; }
{ pkgs, php }:
(pkgs.frankenphp.override { inherit php; }).overrideAttrs (_: {
  phpEmbedWithZts = php;
  phpUnwrapped = php.unwrapped;
  phpConfig = "${php.unwrapped.dev}/bin/php-config";
})
