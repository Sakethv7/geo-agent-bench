// Their signer. Writes headers for two Signature-Agent forms, plus the public key.
const PKG = "file://" + process.env.WBA_PKG + "/web-bot-auth/dist/";
const { sign, generateNonce } = await import(PKG + "index.mjs");
const { signerFromJWK } = await import(PKG + "crypto.mjs");
const kp = await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"]);
const jwk = { ...(await crypto.subtle.exportKey("jwk", kp.privateKey)), alg: "EdDSA" };
const pub = { kty: jwk.kty, crv: jwk.crv, x: jwk.x };
const out = { public: pub, cases: {} };
const forms = {
  "-02 origin with type=directory (their README)": 'sig1="https://bot.example.test";type=directory',
  "-01 key-set URL": 'sig1="https://bot.example.test/.well-known/http-message-signatures-directory"',
};
for (const [name, agent] of Object.entries(forms)) {
  const request = new Request("https://ab12cd34.account.example.test/about", { headers: { "Signature-Agent": agent } });
  const now = new Date();
  const fields = await sign(request, { signer: await signerFromJWK(jwk), created: now, expires: new Date(now.getTime() + 300_000), nonce: generateNonce() });
  out.cases[name] = { "Signature": fields.signature, "Signature-Input": fields.signatureInput, "Signature-Agent": agent };
}
console.log(JSON.stringify(out));
