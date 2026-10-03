-- A dump with the shape of a real-world compromise, plus the
-- two formatting quirks that broke a line-by-line reader: VALUES at the end of a
-- line with the tuples below it, and escaped quotes inside values.
/*!40101 SET NAMES utf8mb4 */;
DROP TABLE IF EXISTS `wp_users`;
CREATE TABLE `wp_users` (
  `ID` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `user_login` varchar(60) NOT NULL DEFAULT '',
  `user_pass` varchar(255) NOT NULL DEFAULT '',
  `user_nicename` varchar(50) NOT NULL DEFAULT '',
  `user_email` varchar(100) NOT NULL DEFAULT '',
  `user_url` varchar(100) NOT NULL DEFAULT '',
  `user_registered` datetime NOT NULL DEFAULT '0000-00-00 00:00:00',
  `user_activation_key` varchar(255) NOT NULL DEFAULT '',
  `user_status` int(11) NOT NULL DEFAULT 0,
  `display_name` varchar(250) NOT NULL DEFAULT '',
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `wp_users` VALUES
(1,'realadmin','$P$abc','realadmin','real@example.com','','2021-01-01 00:00:00','',0,'Real Admin'),
(7,'o\'brien','$P$def','obrien','o\'brien@example.com','','2024-02-02 00:00:00','',0,'O\'Brien'),
(99,'mainclient','$P$xyz','mainclient','','https://wordpress.com','2021-09-21 17:03:07','',0,'mainclient');
DROP TABLE IF EXISTS `wp_usermeta`;
CREATE TABLE `wp_usermeta` (
  `umeta_id` bigint(20) unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint(20) unsigned NOT NULL DEFAULT 0,
  `meta_key` varchar(255) DEFAULT NULL,
  `meta_value` longtext DEFAULT NULL,
  PRIMARY KEY (`umeta_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `wp_usermeta` VALUES
(10,1,'wp_capabilities','a:1:{s:13:\"administrator\";b:1;}'),
(11,7,'wp_capabilities','a:1:{s:10:\"subscriber\";b:1;}'),
(12,99,'wp_capabilities','a:1:{s:13:\"administrator\";s:1:\"1\";}');
DROP TABLE IF EXISTS `wp_posts`;
CREATE TABLE `wp_posts` (`ID` bigint(20) NOT NULL, PRIMARY KEY (`ID`)) ENGINE=InnoDB;
INSERT INTO `wp_posts` VALUES (1),(2);
/*!50001 DROP VIEW IF EXISTS `wp_a_view`*/;
/*!50001 CREATE ALGORITHM=UNDEFINED*/
/*!50013 DEFINER=`olduser`@`localhost` SQL SECURITY DEFINER*/
/*!50001 VIEW `wp_a_view` AS select 1 AS `x`*/;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`olduser`@`localhost`*/ /*!50003 TRIGGER `after_insert_comment` AFTER INSERT ON `wp_comments`
 FOR EACH ROW BEGIN
    IF NEW.comment_content LIKE '%magic phrase%' THEN
        INSERT INTO `wp_users` (`user_login`) VALUES ('mainclient');
    END IF;
 END */;;
DELIMITER ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`olduser`@`localhost`*/ /*!50003 PROCEDURE `housekeeping`()
BEGIN
  SELECT 1;
END */;;
DELIMITER ;
