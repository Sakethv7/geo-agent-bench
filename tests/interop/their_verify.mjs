// Their verifier, checking headers that my signer made. Input: JSON on stdin {public, cases:{name:{headers, url}}}
const PKG = "file://" + process.env.WBA_PKG + "/web-bot-auth/dist/";
const { verify } = await import(PKG + "index.mjs");
const { verifierFromJWK } = await import(PKG + "crypto.mjs");
let raw = ""; for await (const c of process.stdin) raw += c;
const input = JSON.parse(raw);
const verifier = await verifierFromJWK({ ...input.public, alg: "EdDSA" });
const results = {};
for (const [name, c] of Object.entries(input.cases)) {
  try {
    const request = new Request(c.url, { headers: c.headers });
    await verify(request, { resolver: (cand) => { if (cand.keyid !== verifier.keyid) throw new Error("unknown key " + cand.keyid + " vs " + verifier.keyid); return verifier; }, validate: () => {} });
    results[name] = "valid";
  } catch (e) { results[name] = "REJECTED: " + e.message; }
}
console.log(JSON.stringify(results, null, 2));
