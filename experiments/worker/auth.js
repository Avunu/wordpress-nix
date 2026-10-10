/**
 * Shared authentication helpers for the edge Worker's control endpoints.
 */

/**
 * Compare two strings in constant time using SHA-256 digests.
 *
 * @param {string} a
 * @param {string} b
 * @returns {Promise<boolean>}
 */
export async function timingSafeEqual( a, b ) {
	const encoder = new TextEncoder();
	const [ da, db ] = await Promise.all( [
		crypto.subtle.digest( 'SHA-256', encoder.encode( a ) ),
		crypto.subtle.digest( 'SHA-256', encoder.encode( b ) ),
	] );
	const va = new Uint8Array( da );
	const vb = new Uint8Array( db );
	let diff = 0;
	for ( let i = 0; i < va.length; i++ ) {
		diff |= va[ i ] ^ vb[ i ];
	}
	return diff === 0;
}
