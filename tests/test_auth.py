import base64
import json
import unittest

from tests import helpers
from mcpres import auth
from mcpres.auth import AuthError, Authenticator, OidcIssuer

S = helpers.SECRET


def b64(x):
    return base64.urlsafe_b64encode(json.dumps(x).encode()).rstrip(b"=").decode()


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.a = Authenticator(S, "http://iss", "aud-1", clock=self.clock)

    def reject(self, tok, text, scopes=()):
        with self.assertRaises(AuthError) as cm:
            self.a.verify(tok, scopes)
        self.assertIn(text, cm.exception.description)
        return cm.exception

    def test_valid_token(self):
        claims = self.a.verify(self.a.mint("bob", ["mcp", "memory:read"]), ["mcp"])
        self.assertEqual(claims["sub"], "bob")
        self.assertEqual(claims["_scopes"], ["mcp", "memory:read"])

    def test_expired(self):
        tok = self.a.mint(ttl=60)
        self.clock.t += 61
        self.reject(tok, "expired")

    def test_expiry_boundary_is_exclusive(self):
        tok = self.a.mint(ttl=60)
        self.clock.t += 59
        self.a.verify(tok)
        self.clock.t += 1
        self.reject(tok, "expired")

    def test_not_before(self):
        self.reject(self.a.mint(not_before_offset=30), "nbf")

    def test_iat_in_future(self):
        self.reject(self.a.mint(issued_at_offset=30), "iat")

    def test_bad_signature(self):
        h, p, s = self.a.mint().split(".")
        self.reject(".".join([h, p, s[:-4] + ("AAAA" if not s.endswith("AAAA") else "BBBB")]), "signature")
        other = Authenticator(b"another-secret-0123456789abcdef", "http://iss", "aud-1", clock=self.clock).mint()
        self.reject(other, "signature")

    def test_payload_tamper_detected(self):
        h, p, s = self.a.mint(scopes=["mcp"]).split(".")
        claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        claims["scope"] = "mcp memory:write audit:read"
        self.reject(".".join([h, b64(claims), s]), "signature")

    def test_wrong_audience_and_issuer(self):
        self.reject(self.a.mint(audience="nope"), "audience")
        self.reject(self.a.mint(issuer="http://evil"), "issuer")

    def test_audience_list(self):
        claims = {"iss": "http://iss", "sub": "x", "aud": ["a", "aud-1"], "iat": self.clock.t, "exp": self.clock.t + 9}
        self.a.verify(auth.encode_jwt(claims, S))
        claims["aud"] = ["a", "b"]
        self.reject(auth.encode_jwt(claims, S), "audience")

    def test_missing_scope(self):
        e = self.reject(self.a.mint(scopes=["mcp"]), "memory:write", ["mcp", "memory:write"])
        self.assertEqual(e.error, "insufficient_scope")
        self.assertEqual(e.status, 403)

    def test_scope_as_list_claim(self):
        claims = {"iss": "http://iss", "sub": "x", "aud": "aud-1", "iat": self.clock.t, "exp": self.clock.t + 9, "scope": ["mcp", "a"]}
        self.assertEqual(self.a.verify(auth.encode_jwt(claims, S), ["a"])["_scopes"], ["a", "mcp"])

    def test_missing_required_claims(self):
        base = {"iss": "http://iss", "sub": "x", "aud": "aud-1", "iat": self.clock.t, "exp": self.clock.t + 9}
        for drop in ("exp", "iat", "sub", "aud", "iss"):
            c = dict(base)
            del c[drop]
            with self.assertRaises(AuthError):
                self.a.verify(auth.encode_jwt(c, S))
        for key, val in (("exp", "9999999999"), ("exp", True), ("exp", float("inf")), ("nbf", "x")):
            c = dict(base)
            c[key] = val
            with self.assertRaises(AuthError):
                self.a.verify(auth.encode_jwt(c, S))

    def test_alg_none_and_other_algs(self):
        payload = {"iss": "http://iss", "sub": "x", "aud": "aud-1", "iat": self.clock.t, "exp": self.clock.t + 99, "scope": "mcp"}
        for alg in ("none", "None", "HS384", "RS256", "hs256", ""):
            tok = b64({"alg": alg, "typ": "JWT"}) + "." + b64(payload) + "."
            self.reject(tok, "alg")
            tok = b64({"alg": alg, "typ": "JWT"}) + "." + b64(payload) + "." + b64("x").replace("=", "")
            with self.assertRaises(AuthError):
                self.a.verify(tok)

    def test_malformed_tokens(self):
        for bad in ("", "a", "a.b", "a.b.c.d", "...", "!!!.@@@.###", "x" * (auth.MAX_TOKEN_LEN + 1), None, 5, "e30.e30.e30"):
            with self.assertRaises(AuthError):
                self.a.verify(bad)

    def test_non_ascii_token_is_rejected_cleanly(self):
        # regression: found by the token fuzzer - used to raise UnicodeEncodeError instead of AuthError
        h, p, s = self.a.mint().split(".")
        for bad in (h + "." + p[:-1] + "\u00e9." + s, h + "\x00." + p + "." + s, h + " ." + p + "." + s):
            with self.assertRaises(AuthError):
                self.a.verify(bad)

    def test_noncanonical_base64_rejected(self):
        tok = self.a.mint()
        h, p, s = tok.split(".")
        with self.assertRaises(AuthError):
            self.a.verify(h + "=." + p + "." + s)
        self.assertEqual(auth.b64url_decode("QQ"), b"A")  # canonical form
        with self.assertRaises(ValueError):
            auth.b64url_decode("QR")  # same byte, non-zero trailing bits -> rejected

    def test_short_secret_refused(self):
        with self.assertRaises(ValueError):
            Authenticator(b"short", "i")

    def test_constant_time_compare_is_used(self):
        import inspect
        self.assertIn("hmac.compare_digest", inspect.getsource(Authenticator.verify))

    def test_still_valid_for_long_lived_stdio(self):
        claims = self.a.verify(self.a.mint(ttl=10))
        self.assertTrue(self.a.still_valid(claims))
        self.clock.t += 11
        self.assertFalse(self.a.still_valid(claims))


class IssuerTests(unittest.TestCase):
    def setUp(self):
        self.a = Authenticator(S, "http://iss", "mcp-resource-heavy")
        self.i = OidcIssuer(self.a)

    def test_discovery(self):
        d = self.i.discovery_document()
        self.assertEqual(d["issuer"], "http://iss")
        self.assertEqual(d["token_endpoint"], "http://iss/oauth/token")
        self.assertIn("HS256", d["id_token_signing_alg_values_supported"])
        self.assertNotIn("jwks_uri", d)

    def test_client_credentials_post_and_basic(self):
        secret = self.i.client_secret("demo-client")
        st, body = self.i.token_request(None, {"grant_type": "client_credentials", "client_id": "demo-client", "client_secret": secret})
        self.assertEqual(st, 200)
        claims = self.a.verify(body["access_token"], ["mcp", "memory:write"])
        self.assertEqual(claims["sub"], "demo-client")
        basic = "Basic " + base64.b64encode(("demo-client:" + secret).encode()).decode()
        st, body = self.i.token_request(basic, {"grant_type": "client_credentials", "scope": "mcp memory:read"})
        self.assertEqual((st, body["scope"]), (200, "mcp memory:read"))

    def test_rejections(self):
        secret = self.i.client_secret("demo-client")
        base = {"grant_type": "client_credentials", "client_id": "demo-client", "client_secret": secret}
        self.assertEqual(self.i.token_request(None, dict(base, grant_type="password"))[1]["error"], "unsupported_grant_type")
        self.assertEqual(self.i.token_request(None, dict(base, client_secret="x"))[0], 401)
        self.assertEqual(self.i.token_request(None, dict(base, client_id="ghost"))[0], 401)
        self.assertEqual(self.i.token_request(None, {"grant_type": "client_credentials"})[0], 401)
        self.assertEqual(self.i.token_request("Basic !!!", {"grant_type": "client_credentials"})[0], 401)
        ro = {"grant_type": "client_credentials", "client_id": "readonly-client", "client_secret": self.i.client_secret("readonly-client")}
        self.assertEqual(self.i.token_request(None, dict(ro, scope="mcp memory:write"))[1]["error"], "invalid_scope")
        self.assertEqual(self.i.token_request(None, ro)[1]["scope"], "mcp memory:read")

    def test_secret_loading(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        s1 = auth.load_or_create_secret(d)
        s2 = auth.load_or_create_secret(d)
        self.assertEqual(s1, s2)
        self.assertEqual(oct(os.stat(os.path.join(d, "oidc_secret")).st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
