'use strict';
// ---------------------------------------------------------------------------
// WorkBuddy field-decryption helper.
//
// Derived from the MIT-licensed community project
//   https://github.com/maureen2215-ai/workbuddy-auto-claim-public
// (originally written for that project's `background.py`). Reused here under
// that license, with the header comment adjusted to this repository. See NOTICE.
//
// Contract with the caller:
//   * invoked as `ELECTRON_RUN_AS_NODE=1 WorkBuddy.exe wb_desktop_session.cjs --private-pipe`
//   * stdin : the JSON envelope {"$wbEncrypted":1,"envelope":"<base64>"}
//   * stdout: the decrypted accessToken, written once to a private pipe
//             (refuses to run when stdout is a TTY, so a token can never be
//              echoed into a terminal or a captured console)
//   * errors: bare exit codes, never any token or key material
//
// Why the client binary is required: the at-rest key is not stored on disk. It
// comes from Electron's internal binding
// `process._linkedBinding('electron_browser_workbuddy_storage').loggerGet()`,
// which returns {version, atRestSecretKey, atRestDeveloperPublicKey}. The
// derived key is sha256(atRestSecretKey), and sha256(key).hex[:16] must match
// envelope.keyId — that comparison is a deterministic oracle, and the reason
// brute-forcing candidate keys on disk is provably fruitless.
// ---------------------------------------------------------------------------

const crypto = require('node:crypto');

function exact(value, keys) {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).sort().join(',') === [...keys].sort().join(',');
}
function base64(value, size) {
  if (typeof value !== 'string' || value.length > 65536) throw Error();
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (size !== undefined && bytes.length !== size)) throw Error();
  return bytes;
}
function uint32(value) {
  const b = Buffer.alloc(4); b.writeUInt32BE(value); return b;
}
function string(value) {
  const b = Buffer.from(value, 'utf8'); return Buffer.concat([uint32(b.length), b]);
}
function decrypt(wrapper, getPayload) {
  if (!exact(wrapper, ['$wbEncrypted', 'envelope']) || wrapper.$wbEncrypted !== 1) throw Error();
  const envelope = JSON.parse(base64(wrapper.envelope).toString('utf8'));
  if (!exact(envelope, ['suite', 'keyId', 'nonce', 'authTag', 'ciphertext']) ||
      envelope.suite !== 1 || !/^[0-9a-f]{16}$/.test(envelope.keyId)) throw Error();
  const nonce = base64(envelope.nonce, 12), tag = base64(envelope.authTag, 16);
  const ciphertext = base64(envelope.ciphertext);
  if (!ciphertext.length || ciphertext.length > 32768) throw Error();
  const payload = JSON.parse(getPayload());
  if (payload.version !== 1) throw Error();
  const secret = base64(payload.atRestSecretKey, 32);
  if (secret.every(v => v === 0)) { secret.fill(0); throw Error(); }
  const key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
  secret.fill(0);
  let plain;
  try {
    const id = crypto.createHash('sha256').update(key).digest('hex').slice(0, 16);
    if (id !== envelope.keyId) throw Error();
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      string('WBEV1'), string('sym-v1'), uint32(1), string(id), Buffer.from([2, 0, 0])]);
    const decipher = crypto.createDecipheriv('aes-256-gcm', key, nonce, {authTagLength: 16});
    decipher.setAAD(aad); decipher.setAuthTag(tag);
    plain = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
    const token = plain.toString('utf8');
    if (!/^[A-Za-z0-9._~+/-]{1,32768}=*$/.test(token)) throw Error();
    return token;
  } finally { key.fill(0); if (plain) plain.fill(0); }
}

module.exports = {decrypt};
if (require.main === module) {
  if (process.argv[2] !== '--private-pipe' || process.stdout.isTTY) process.exit(2);
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => {
    input += chunk;
    if (input.length > 65536) process.exit(2);
  });
  process.stdin.on('end', () => {
    try {
      const token = decrypt(JSON.parse(input), () =>
        process._linkedBinding('electron_browser_workbuddy_storage').loggerGet());
      process.stdout.write(token);
    } catch { process.exitCode = 2; }
  });
}
