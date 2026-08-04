<?php
/**
 * mysql-to-sqlite — convert a MySQL dump into a SQLite database through the
 * MySQL-on-SQLite driver.
 *
 * The driver IS the MySQL→SQLite translator that will serve the site at
 * runtime, so replaying a dump through it (rather than through a generic
 * converter) produces exactly the schema the site expects — including the
 * driver's emulated INFORMATION_SCHEMA tables, with full type fidelity for
 * plugin tables as well as core ones.
 *
 * Statements are split with the project's own MySQL lexer, so semicolons
 * inside string literals, comments, and quoted identifiers never split a
 * statement (a naive split on ";" corrupts real WordPress content).
 *
 * Usage:
 *   mysql-to-sqlite <dump.sql> <output.sqlite> [--db-name=wordpress]
 *                                              [--continue-on-error]
 *                                              [--quiet]
 *
 * Exit codes: 0 success · 1 usage/IO error · 2 conversion error.
 *
 * @package wordpress-nix
 */

declare( strict_types = 1 );

// The driver source is injected by the Nix wrapper; fall back to a sibling
// checkout so the script is runnable from a dev tree.
$driver_root = getenv( 'WP_MYSQL_ON_SQLITE_SRC' );
if ( false === $driver_root || '' === $driver_root ) {
	$driver_root = __DIR__ . '/../../sqlite-database-integration/packages/mysql-on-sqlite/src';
}
if ( ! file_exists( $driver_root . '/load.php' ) ) {
	fwrite( STDERR, "error: MySQL-on-SQLite driver not found at {$driver_root}\n" );
	fwrite( STDERR, "       set WP_MYSQL_ON_SQLITE_SRC to the driver's src/ directory.\n" );
	exit( 1 );
}
require_once $driver_root . '/load.php';

/**
 * Resolve the driver's PDO class.
 *
 * The class was renamed from WP_PDO_MySQL_On_SQLite to WP_MySQL_On_SQLite
 * upstream; support both so the converter works against any pinned driver
 * revision the platform is built with.
 *
 * @return string The class name.
 */
function m2s_driver_class(): string {
	foreach ( array( 'WP_MySQL_On_SQLite', 'WP_PDO_MySQL_On_SQLite' ) as $class ) {
		if ( class_exists( $class, false ) ) {
			return $class;
		}
	}
	fwrite( STDERR, "error: the driver's PDO class was not found in the loaded driver source.\n" );
	exit( 1 );
}

/**
 * Parse command-line arguments.
 *
 * @param  array $argv Raw arguments.
 * @return array{input:string,output:string,db_name:string,continue:bool,quiet:bool}
 */
function m2s_parse_args( array $argv ): array {
	$positional = array();
	$db_name    = 'wordpress';
	$continue   = false;
	$quiet      = false;

	foreach ( array_slice( $argv, 1 ) as $arg ) {
		if ( 0 === strpos( $arg, '--db-name=' ) ) {
			$db_name = substr( $arg, 10 );
		} elseif ( '--continue-on-error' === $arg ) {
			$continue = true;
		} elseif ( '--quiet' === $arg || '-q' === $arg ) {
			$quiet = true;
		} elseif ( '--help' === $arg || '-h' === $arg ) {
			m2s_usage( 0 );
		} elseif ( 0 === strpos( $arg, '-' ) ) {
			fwrite( STDERR, "error: unknown option {$arg}\n" );
			m2s_usage( 1 );
		} else {
			$positional[] = $arg;
		}
	}

	if ( count( $positional ) !== 2 ) {
		m2s_usage( 1 );
	}

	return array(
		'input'    => $positional[0],
		'output'   => $positional[1],
		'db_name'  => $db_name,
		'continue' => $continue,
		'quiet'    => $quiet,
	);
}

/**
 * Print usage and exit.
 *
 * @param int $code Exit code.
 */
function m2s_usage( int $code ): void {
	$out = 0 === $code ? STDOUT : STDERR;
	fwrite(
		$out,
		"usage: mysql-to-sqlite <dump.sql> <output.sqlite> [options]\n\n"
		. "  --db-name=<name>       Logical database name (default: wordpress)\n"
		. "  --continue-on-error    Report failed statements but keep going\n"
		. "  --quiet, -q            Only print the final summary\n"
	);
	exit( $code );
}

/**
 * Split a MySQL script into individual statements using the driver's lexer.
 *
 * Splitting on the lexer's SEMICOLON tokens (rather than on raw ";") keeps
 * semicolons inside strings, comments, and quoted identifiers intact.
 *
 * @param  string $sql The script.
 * @return string[] The statements, without trailing semicolons.
 */
function m2s_split_statements( string $sql ): array {
	$lexer      = new WP_MySQL_Lexer( $sql );
	$statements = array();
	$start      = 0;

	while ( $lexer->next_token() ) {
		$token = $lexer->get_token();
		if ( null === $token || WP_MySQL_Lexer::EOF === $token->id ) {
			break;
		}
		if ( WP_MySQL_Lexer::SEMICOLON_SYMBOL !== $token->id ) {
			continue;
		}

		$statement = trim( substr( $sql, $start, $token->start - $start ) );
		if ( '' !== $statement ) {
			$statements[] = $statement;
		}
		$start = $token->start + $token->length;
	}

	// A trailing statement without a closing semicolon.
	$tail = trim( substr( $sql, $start ) );
	if ( '' !== $tail ) {
		$statements[] = $tail;
	}

	return $statements;
}

/**
 * Whether a statement should be skipped rather than replayed.
 *
 * mysqldump emits session setup, locking, and MySQL-specific directives that
 * are meaningless (or unsupported) on SQLite. Transaction control is skipped
 * because the converter manages one transaction for the whole load.
 *
 * @param  string $statement The statement.
 * @return bool True when the statement should be skipped.
 */
function m2s_should_skip( string $statement ): bool {
	// Conditional-execution comments: /*!40101 SET ... */ — MySQL-only.
	if ( 0 === strpos( $statement, '/*!' ) ) {
		return true;
	}

	$normalized = ltrim( $statement );
	// Strip leading comment lines to reach the real keyword.
	while ( 0 === strpos( $normalized, '--' ) || 0 === strpos( $normalized, '#' ) ) {
		$newline    = strpos( $normalized, "\n" );
		$normalized = false === $newline ? '' : ltrim( substr( $normalized, $newline + 1 ) );
	}
	if ( 0 === strpos( $normalized, '/*' ) ) {
		$end        = strpos( $normalized, '*/' );
		$normalized = false === $end ? '' : ltrim( substr( $normalized, $end + 2 ) );
	}

	$skip_prefixes = array(
		'SET ',
		'LOCK TABLES',
		'UNLOCK TABLES',
		'START TRANSACTION',
		'BEGIN',
		'COMMIT',
		'ROLLBACK',
		'USE ',
		'CREATE DATABASE',
		'DROP DATABASE',
		'ALTER DATABASE',
		'FLUSH ',
		'DELIMITER',
	);
	$upper         = strtoupper( $normalized );
	foreach ( $skip_prefixes as $prefix ) {
		if ( 0 === strpos( $upper, $prefix ) ) {
			return true;
		}
	}

	return '' === $normalized;
}

// --- main ---------------------------------------------------------------

$args = m2s_parse_args( $argv );

if ( ! is_readable( $args['input'] ) ) {
	fwrite( STDERR, "error: cannot read {$args['input']}\n" );
	exit( 1 );
}
if ( file_exists( $args['output'] ) ) {
	fwrite( STDERR, "error: {$args['output']} already exists — refusing to overwrite.\n" );
	exit( 1 );
}

$sql = file_get_contents( $args['input'] );
if ( false === $sql ) {
	fwrite( STDERR, "error: failed to read {$args['input']}\n" );
	exit( 1 );
}

$log = function ( string $message ) use ( $args ): void {
	if ( ! $args['quiet'] ) {
		fwrite( STDERR, $message . "\n" );
	}
};

$log( sprintf( 'Reading %s (%s)', $args['input'], m2s_format_bytes( strlen( $sql ) ) ) );
$statements = m2s_split_statements( $sql );
$log( sprintf( 'Parsed %d statements', count( $statements ) ) );

$driver_class = m2s_driver_class();
try {
	$driver = new $driver_class(
		sprintf(
			'mysql-on-sqlite:path=%s;dbname=%s',
			str_replace( ';', ';;', $args['output'] ),
			str_replace( ';', ';;', $args['db_name'] )
		)
	);
} catch ( Throwable $e ) {
	fwrite( STDERR, 'error: failed to open the SQLite database: ' . $e->getMessage() . "\n" );
	exit( 2 );
}

$executed = 0;
$skipped  = 0;
$failed   = 0;
$errors   = array();
$total    = count( $statements );

foreach ( $statements as $index => $statement ) {
	if ( m2s_should_skip( $statement ) ) {
		++$skipped;
		continue;
	}

	try {
		$driver->query( $statement );
		++$executed;
	} catch ( Throwable $e ) {
		++$failed;
		$summary  = preg_replace( '/\s+/', ' ', substr( $statement, 0, 120 ) );
		$errors[] = sprintf( '  [%d] %s: %s', $index + 1, $summary, $e->getMessage() );
		if ( ! $args['continue'] ) {
			fwrite( STDERR, "\nerror: statement " . ( $index + 1 ) . " failed:\n" );
			fwrite( STDERR, end( $errors ) . "\n" );
			fwrite( STDERR, "\nRe-run with --continue-on-error to convert everything else and\n" );
			fwrite( STDERR, "see the full list of failures.\n" );
			exit( 2 );
		}
	}

	if ( 0 === ( $executed % 500 ) && $executed > 0 ) {
		$log( sprintf( '  %d/%d statements', $index + 1, $total ) );
	}
}

$log(
	sprintf(
		'Converted: %d executed, %d skipped (MySQL session/transaction directives), %d failed',
		$executed,
		$skipped,
		$failed
	)
);

if ( $failed > 0 ) {
	fwrite( STDERR, "\nFailed statements:\n" . implode( "\n", array_slice( $errors, 0, 50 ) ) . "\n" );
	if ( count( $errors ) > 50 ) {
		fwrite( STDERR, sprintf( "  ... and %d more\n", count( $errors ) - 50 ) );
	}
}

// Report what landed, so the caller can sanity-check before loading into D1.
try {
	$result = $driver->query( 'SHOW TABLES' );
	$tables = $result ? $result->fetchAll( PDO::FETCH_COLUMN ) : array();
	$log( sprintf( 'Tables: %d', count( $tables ) ) );
	if ( ! $args['quiet'] ) {
		foreach ( $tables as $table ) {
			$count = $driver->query( 'SELECT COUNT(*) FROM `' . str_replace( '`', '``', $table ) . '`' );
			$rows  = $count ? (int) $count->fetchColumn() : 0;
			fwrite( STDERR, sprintf( "  %-40s %8d rows\n", $table, $rows ) );
		}
	}
} catch ( Throwable $e ) {
	fwrite( STDERR, 'warning: could not summarize tables: ' . $e->getMessage() . "\n" );
}

fwrite( STDOUT, $args['output'] . "\n" );
exit( $failed > 0 && ! $args['continue'] ? 2 : 0 );

/**
 * Human-readable byte size.
 *
 * @param  int $bytes The size.
 * @return string
 */
function m2s_format_bytes( int $bytes ): string {
	$units = array( 'B', 'KiB', 'MiB', 'GiB' );
	$i     = 0;
	$size  = (float) $bytes;
	while ( $size >= 1024 && $i < count( $units ) - 1 ) {
		$size /= 1024;
		++$i;
	}
	return sprintf( '%.1f %s', $size, $units[ $i ] );
}
