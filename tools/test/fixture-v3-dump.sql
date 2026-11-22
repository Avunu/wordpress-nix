-- The §8.4 cases on top of the compromise fixture: an event, account statements,
-- the credentials that survive a move (application passwords, session tokens,
-- MainWP's options), an obfuscated autoloaded option, a widget carrying a script,
-- a snippet table row, a home/siteurl mismatch, and an administrator the survey
-- does not list. Prefix wpx_.
/*!40101 SET NAMES utf8mb4 */;
CREATE USER 'backdoor'@'%' IDENTIFIED BY 'x';
GRANT ALL PRIVILEGES ON *.* TO 'backdoor'@'%';
DROP TABLE IF EXISTS `wpx_options`;
CREATE TABLE `wpx_options` (`option_id` bigint, `option_name` varchar(191), `option_value` longtext, `autoload` varchar(20)) ENGINE=InnoDB;
INSERT INTO `wpx_options` VALUES (1,'siteurl','https://legacy.example','yes'),(2,'home','https://other.example','yes'),(3,'mainwp_child_pubkey','LS0tLS1CRUdJTg==','yes'),(4,'blogname','Keep; me\'s title','yes'),(5,'wp_cache_x','eval(base64_decode(\'ZWNobyAxOw==\'));','yes'),(6,'widget_custom_html','a:1:{i:2;a:1:{s:7:\"content\";s:30:\"<script src=\\\"//evil\\\"></script>\";}}','yes'),(7,'mainwp_child_uniqueId','abc','yes'),(8,'lazy_big','eval(1)','no');
DROP TABLE IF EXISTS `wpx_usermeta`;
CREATE TABLE `wpx_usermeta` (`umeta_id` bigint, `user_id` bigint, `meta_key` varchar(255), `meta_value` longtext) ENGINE=InnoDB;
INSERT INTO `wpx_usermeta` VALUES
(10,1,'wpx_capabilities','a:1:{s:13:\"administrator\";b:1;}'),
(11,1,'_application_passwords','a:1:{i:0;a:1:{s:4:\"uuid\";s:3:\"abc\";}}'),
(12,1,'session_tokens','a:1:{s:3:\"tok\";a:0:{}}'),
(13,1,'nickname','o\'admin, (the) first'),
(14,5,'wpx_capabilities','a:1:{s:13:\"administrator\";b:1;}');
INSERT INTO `wpx_usermeta` VALUES (15,5,'session_tokens','a:0:{}');
DROP TABLE IF EXISTS `wpx_users`;
CREATE TABLE `wpx_users` (`ID` bigint, `user_login` varchar(60), `user_pass` varchar(255), `user_nicename` varchar(50), `user_email` varchar(100), `user_url` varchar(100), `user_registered` datetime) ENGINE=InnoDB;
INSERT INTO `wpx_users` VALUES (1,'admin','$P$a','admin','admin@legacy.example','','2020-01-01 00:00:00'),(5,'newcomer','$P$b','newcomer','new@evil.example','','2026-11-01 00:00:00');
DROP TABLE IF EXISTS `wpx_wpcb_snippets`;
CREATE TABLE `wpx_wpcb_snippets` (`id` bigint, `title` varchar(255), `code` longtext) ENGINE=InnoDB;
INSERT INTO `wpx_wpcb_snippets` VALUES (3,'Footer tweak','add_action(\'wp_footer\', function () { echo 1; });');
DELIMITER ;;
/*!50106 CREATE*/ /*!50117 DEFINER=`olduser`@`localhost`*/ /*!50106 EVENT `nightly_admin` ON SCHEDULE EVERY 1 DAY DO UPDATE `wpx_usermeta` SET meta_value = 'a:1:{s:13:"administrator";b:1;}' WHERE user_id = 5 */ ;;
DELIMITER ;
