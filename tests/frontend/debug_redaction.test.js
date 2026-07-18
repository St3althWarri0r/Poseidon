/* Pure-function unit test for the debug console's redaction (spec task 8a).
 *
 * There is no JS test harness in-repo, so this is a node one-off: it `require`s
 * debug.js (which, under node, skips its browser install() and only exports the
 * pure functions) and asserts the LOAD-BEARING secret-redaction invariant —
 * secrets never enter the ring buffer. Run directly (`node debug_redaction.test.js`)
 * or via the pytest wrapper tests/unit/test_debug_redaction.py, which keeps it in
 * the CI gate. Exit 0 = pass, non-zero = fail (assert throws).
 */
"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");

const debug = require(
  path.join(__dirname, "..", "..", "src", "poseidon", "api", "static", "debug.js")
);
const { redactUrl, redactBody } = debug;

/* ---- redactBody: request/response object bodies ---- */

// The response of /api/brokers/schwab/exchange carries refresh_token; the
// request of /api/brokers/connect carries broker creds — both must be redacted.
const walked = redactBody({
  refresh_token: "1//super-secret",
  app_secret: "shh",
  app_key: "ak_live",
  password: "hunter2",
  passphrase: "open sesame",
  authorization: "Bearer nope",
  credential: "c",
  api_key: "live_123",
  nested: { access_token: "deep", note: "keep me" },
  arr: [{ secret: "s" }, { fine: "ok" }],
  keep: "visible",
});
for (const k of [
  "refresh_token", "app_secret", "app_key", "password", "passphrase",
  "authorization", "credential", "api_key",
]) {
  assert.equal(walked[k], "REDACTED", `top-level "${k}" must be REDACTED`);
}
assert.equal(walked.nested.access_token, "REDACTED", "nested secret must be REDACTED");
assert.equal(walked.nested.note, "keep me", "nested non-secret must survive");
assert.equal(walked.arr[0].secret, "REDACTED", "secret inside array must be REDACTED");
assert.equal(walked.arr[1].fine, "ok", "non-secret inside array must survive");
assert.equal(walked.keep, "visible", "non-secret field must survive");

// no secret VALUE may appear anywhere in the serialized result
const walkedStr = JSON.stringify(walked);
for (const leak of [
  "super-secret", "shh", "ak_live", "hunter2", "open sesame", "Bearer nope",
  "live_123", "deep",
]) {
  assert.ok(!walkedStr.includes(leak), `secret value "${leak}" leaked into output`);
}

/* ---- redactBody: JSON string body is parsed then walked ---- */

const fromString = redactBody('{"api_key":"live_123","ok":1}');
assert.equal(fromString.api_key, "REDACTED", "JSON-string secret must be REDACTED");
assert.equal(fromString.ok, 1, "JSON-string non-secret must survive");

/* ---- redactBody: non-JSON bodies stored as size only, never raw ---- */

assert.equal(
  redactBody("token=abcXYZsecret&grant_type=refresh"),
  "[body 37 bytes]",
  "non-JSON string body must become a size placeholder, never raw"
);
assert.equal(redactBody(null), null, "no body → null passes through");

/* ---- redactUrl: secret query-param values replaced ---- */

assert.equal(redactUrl("/ws?token=abc123&x=1"), "/ws?token=REDACTED&x=1");
assert.equal(redactUrl("/api?api_key=live_123"), "/api?api_key=REDACTED");
assert.equal(
  redactUrl("/api?token=a&refresh_token=b&keep=c"),
  "/api?token=REDACTED&refresh_token=REDACTED&keep=c"
);
assert.equal(redactUrl("/api/cycle"), "/api/cycle", "no query → unchanged");
// fragment is preserved after a redacted query
assert.equal(redactUrl("/p?secret=s#frag"), "/p?secret=REDACTED#frag");

console.log("debug redaction: all assertions passed");
