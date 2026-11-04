"""Interop with an independent implementation: Cloudflare's `web-bot-auth` package for Node (ADR-025).

The package is not vendored. Install it into a scratch folder without running its install scripts:
    npm install web-bot-auth --ignore-scripts --prefix <dir>
then point WBA_PKG at <dir>/node_modules. Without it these tests skip. They use a throwaway key and make no
network request.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from geobench.webbotauth import Verifier, public_jwk, sign_headers

PKG = os.environ.get("WBA_PKG")
pytestmark = pytest.mark.skipif(not PKG or not shutil.which("node"), reason="set WBA_PKG and install node to run the interop tests")
HERE = Path(__file__).parent / "interop"
AUTHORITY = "ab12cd34.account.example.test"


def node(script: str, stdin: str = "") -> str:
    done = subprocess.run(["node", str(HERE / script)], input=stdin, capture_output=True, text=True, env={**os.environ, "WBA_PKG": PKG}, timeout=60)
    assert done.returncode == 0, done.stderr[:500]
    return done.stdout


def test_their_signer_verifies_on_my_verifier():
    signed = json.loads(node("their_sign.mjs"))
    verifier = Verifier(fetch=lambda url: {"keys": [signed["public"]]}, url_ok=lambda url: True)
    assert len(signed["cases"]) == 2
    for name, headers in signed["cases"].items():
        result = verifier.verify(headers, AUTHORITY, "GET", "/about")
        assert result.verdict == "valid", (name, result)


def test_my_signer_verifies_on_their_verifier():
    key = Ed25519PrivateKey.generate()
    forms = ("url", "origin", "origin_typed", "legacy")
    cases = {f: {"url": f"https://{AUTHORITY}/about", "headers": sign_headers(key, "https://bot.example.test/", AUTHORITY, form=f)} for f in forms}
    results = json.loads(node("their_verify.mjs", json.dumps({"public": public_jwk(key), "cases": cases})))
    assert results == {f: "valid" for f in forms}
