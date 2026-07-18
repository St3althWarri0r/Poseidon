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
const { redactUrl, redactBody, summarize } = debug;

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
// OAuth redirect params (code/session/bare key) are redacted, not just the
// SECRET_KEY-named ones — these are the ones an authorization callback carries.
assert.equal(
  redactUrl("https://127.0.0.1/cb?code=AUTHCODE&session=SESS&key=K&keep=x"),
  "https://127.0.0.1/cb?code=REDACTED&session=REDACTED&key=REDACTED&keep=x"
);

/* ---- redactBody: a STRING VALUE that is itself a secret-bearing URL ---- */

// Schwab's exchange request carries the whole callback URL in `redirect_response`
// — a field name SECRET_KEY does not match — so its ?code=/&session= query would
// otherwise leak. The body walk must route string values through redactUrl.
const redirectBody = redactBody(JSON.stringify({
  broker: "schwab",
  app_key: "AK_pub_ok",
  app_secret: "AS_SECRET_zzz",
  refresh_token: "RT_SECRET_qqq",
  redirect_response: "https://127.0.0.1/?code=AUTHCODE_SECRET&session=SESS_SECRET",
}));
assert.equal(redirectBody.app_secret, "REDACTED", "app_secret key still redacts");
assert.equal(redirectBody.refresh_token, "REDACTED", "refresh_token key still redacts");
assert.equal(
  redirectBody.redirect_response,
  "https://127.0.0.1/?code=REDACTED&session=REDACTED",
  "URL-valued field's code/session query params must be redacted"
);
const redirectStr = JSON.stringify(redirectBody);
for (const leak of ["AS_SECRET_zzz", "RT_SECRET_qqq", "AUTHCODE_SECRET", "SESS_SECRET"]) {
  assert.ok(!redirectStr.includes(leak), `secret "${leak}" leaked from URL-valued field`);
}

/* ---- summarize: a NON-JSON response body is size-only, never raw ---- */

function fakeResponse(text) {           // minimal Response for summarize()
  return { clone() { return { async text() { return text; } }; } };
}
(async () => {
  const nonJson = await summarize(fakeResponse("refresh_token=LEAKME"));
  assert.equal(nonJson, "[body 20 bytes]",
    "non-JSON response must be a size placeholder, never raw-echoed");
  assert.ok(!nonJson.includes("LEAKME"), "non-JSON response body must not raw-echo secrets");
  // the JSON path still runs the redaction walk unchanged
  const jsonSummary = await summarize(fakeResponse(JSON.stringify({ refresh_token: "RT_x", ok: 1 })));
  assert.ok(!jsonSummary.includes("RT_x"), "JSON response secret must still redact");
  assert.ok(jsonSummary.includes("REDACTED"), "JSON response redaction walk still runs");

  console.log("debug redaction: all assertions passed");
})();
