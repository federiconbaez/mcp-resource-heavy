"""Simplified OIDC-style auth: HS256 JWTs + a tiny mock issuer.

THIS IS NOT FULL OIDC AND IS NOT CERTIFIED. What it is:

* HS256 JSON Web Tokens implemented by hand with hmac/hashlib/base64
  (RFC 7519 / RFC 7515 subset). Symmetric: the verifier and the issuer share one
  secret, so there is no JWKS and no key rotation.
* An OIDC-*like* discovery document and an OAuth2 ``client_credentials``-style
  token endpoint (RFC 6749 section 4.4) with client_secret_basic / client_secret_post.
* Validation of ``iss``, ``aud``, ``exp``, ``nbf``, ``iat``, signature
  (constant-time compare) and ``scope``.

Not implemented: authorization-code flow, id_tokens for end users, PKCE,
refresh tokens, JWKS/asymmetric keys, token revocation, dynamic client registration.
"""

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time

ALG = "HS256"
MAX_TOKEN_LEN = 8192
B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BASE_SCOPE = "mcp"
ALL_SCOPES = ("mcp", "memory:read", "memory:write", "audit:read")
DEFAULT_AUDIENCE = "mcp-resource-heavy"
DEFAULT_ISSUER = "http://127.0.0.1:8765"

# Demo client registry for the mock issuer: client_id -> scopes it may request.
# Secrets are *derived* from the server secret (see OidcIssuer.client_secret) so nothing is stored.
DEFAULT_CLIENTS = {
    "demo-client": ("mcp", "memory:read", "memory:write", "audit:read"),
    "readonly-client": ("mcp", "memory:read"),
}


class AuthError(Exception):
    """Token or credential rejected. ``error`` follows RFC 6750 / RFC 6749 names."""

    def __init__(self, error, description, status=401):
        super().__init__("%s: %s" % (error, description))
        self.error = error
        self.description = description
        self.status = status


# ---- base64url (strict, canonical) ---------------------------------------
def b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text):
    if not isinstance(text, str) or not B64URL_RE.match(text) or len(text) % 4 == 1:
        raise ValueError("invalid base64url")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("invalid base64url")
    if b64url_encode(raw) != text:  # reject non-canonical trailing bits
        raise ValueError("non-canonical base64url")
    return raw


# ---- JWT -----------------------------------------------------------------
def _sign(secret, signing_input):
    return hmac.new(secret, signing_input, hashlib.sha256).digest()


def _json_b64(obj):
    return b64url_encode(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def encode_jwt(claims, secret):
    header = {"alg": ALG, "typ": "JWT"}
    signing_input = ("%s.%s" % (_json_b64(header), _json_b64(claims))).encode("ascii")
    return signing_input.decode("ascii") + "." + b64url_encode(_sign(secret, signing_input))


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_scopes(claim):
    if isinstance(claim, str):
        return set(claim.split())
    if isinstance(claim, list) and all(isinstance(x, str) for x in claim):
        return set(claim)
    return set()


class Authenticator:
    """Validates tokens minted with the shared secret."""

    def __init__(self, secret, issuer, audience=DEFAULT_AUDIENCE, leeway=0, clock=time.time):
        if not isinstance(secret, (bytes, bytearray)) or len(secret) < 16:
            raise ValueError("secret must be at least 16 bytes")
        self.secret = bytes(secret)
        self.issuer = issuer
        self.audience = audience
        self.leeway = leeway
        self.clock = clock

    def mint(self, subject="demo-user", scopes=ALL_SCOPES, ttl=3600, audience=None, issuer=None,
             not_before_offset=0, issued_at_offset=0):
        now = int(self.clock())
        claims = {
            "iss": issuer or self.issuer,
            "sub": subject,
            "aud": audience or self.audience,
            "iat": now + issued_at_offset,
            "nbf": now + not_before_offset,
            "exp": now + int(ttl),
            "scope": " ".join(scopes),
            "jti": secrets.token_hex(8),
        }
        return encode_jwt(claims, self.secret)

    def verify(self, token, required_scopes=()):
        """Return the claims dict or raise AuthError."""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LEN:
            raise AuthError("invalid_token", "token missing or too long")
        if not TOKEN_RE.match(token):
            raise AuthError("invalid_token", "token contains characters outside the base64url/JWS alphabet")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("invalid_token", "token is not a compact JWS (3 parts)")
        try:
            header = json.loads(b64url_decode(parts[0]).decode("utf-8"))
            signature = b64url_decode(parts[2])
        except (ValueError, UnicodeDecodeError):
            raise AuthError("invalid_token", "malformed token encoding")
        if not isinstance(header, dict) or header.get("alg") != ALG:
            raise AuthError("invalid_token", "unsupported or missing alg (only HS256 is accepted)")
        expected = _sign(self.secret, (parts[0] + "." + parts[1]).encode("ascii"))
        if not hmac.compare_digest(expected, signature):
            raise AuthError("invalid_token", "signature verification failed")
        try:
            claims = json.loads(b64url_decode(parts[1]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise AuthError("invalid_token", "malformed claims")
        if not isinstance(claims, dict):
            raise AuthError("invalid_token", "claims must be an object")
        now = self.clock()
        if claims.get("iss") != self.issuer:
            raise AuthError("invalid_token", "wrong issuer")
        aud = claims.get("aud")
        auds = [aud] if isinstance(aud, str) else (aud if isinstance(aud, list) else [])
        if self.audience not in auds:
            raise AuthError("invalid_token", "wrong audience")
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise AuthError("invalid_token", "missing sub")
        if not _num(claims.get("exp")):
            raise AuthError("invalid_token", "missing exp")
        if now >= claims["exp"] + self.leeway:
            raise AuthError("invalid_token", "token expired")
        if not _num(claims.get("iat")):
            raise AuthError("invalid_token", "missing iat")
        if claims["iat"] > now + self.leeway:
            raise AuthError("invalid_token", "iat is in the future")
        if "nbf" in claims:
            if not _num(claims["nbf"]):
                raise AuthError("invalid_token", "bad nbf")
            if claims["nbf"] > now + self.leeway:
                raise AuthError("invalid_token", "token not yet valid (nbf)")
        granted = parse_scopes(claims.get("scope"))
        missing = [s for s in required_scopes if s not in granted]
        if missing:
            raise AuthError("insufficient_scope", "missing scope: " + " ".join(missing), status=403)
        claims["_scopes"] = sorted(granted)
        return claims

    def still_valid(self, claims):
        """Cheap re-check used on long-lived stdio sessions: has exp passed since initialize?"""
        return self.clock() < claims["exp"] + self.leeway


def load_or_create_secret(data_dir):
    """Secret from $MCP_FORGE_SECRET, else <data_dir>/oidc_secret (created 0600)."""
    env = os.environ.get("MCP_FORGE_SECRET")
    if env:
        if len(env.encode("utf-8")) < 16:
            raise ValueError("MCP_FORGE_SECRET must be at least 16 bytes")
        return env.encode("utf-8")
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    path = os.path.join(data_dir, "oidc_secret")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process may still be writing it; wait briefly for content.
        for _ in range(50):
            with open(path, "rb") as fh:
                data = fh.read().strip()
            if len(data) >= 16:
                return data
            time.sleep(0.02)
        raise ValueError("secret file exists but is empty/short: %s" % path)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secrets.token_hex(32).encode("ascii"))
    with open(path, "rb") as fh:
        return fh.read().strip()


class OidcIssuer:
    """Mock issuer: discovery document + client_credentials token endpoint."""

    def __init__(self, authenticator, clients=None, default_ttl=3600):
        self.auth = authenticator
        self.clients = dict(clients or DEFAULT_CLIENTS)
        self.default_ttl = default_ttl

    def client_secret(self, client_id):
        digest = hmac.new(self.auth.secret, b"mcpres-client:" + client_id.encode("utf-8"), hashlib.sha256)
        return digest.hexdigest()[:32]

    def discovery_document(self):
        iss = self.auth.issuer.rstrip("/")
        return {
            "issuer": self.auth.issuer,
            "token_endpoint": iss + "/oauth/token",
            "grant_types_supported": ["client_credentials"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "response_types_supported": [],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": [ALG],
            "scopes_supported": list(ALL_SCOPES),
            "x-mcp-resource-heavy-note": "simplified mock issuer: HS256 shared-secret, no JWKS, not OIDC-certified",
        }

    def token_request(self, authorization_header, form):
        """Returns (http_status, body_dict). ``form`` is a dict of str -> str."""
        if form.get("grant_type") != "client_credentials":
            return 400, {"error": "unsupported_grant_type", "error_description": "only client_credentials"}
        client_id, client_secret = None, None
        if authorization_header and authorization_header.lower().startswith("basic "):
            try:
                raw = base64.b64decode(authorization_header[6:].strip(), validate=True).decode("utf-8")
                client_id, _, client_secret = raw.partition(":")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                return 401, {"error": "invalid_client", "error_description": "malformed Basic credentials"}
        else:
            client_id, client_secret = form.get("client_id"), form.get("client_secret")
        if not client_id or client_secret is None:
            return 401, {"error": "invalid_client", "error_description": "client authentication required"}
        known = client_id in self.clients
        expected = self.client_secret(client_id if known else "unknown")
        ok = hmac.compare_digest(expected.encode("utf-8"), client_secret.encode("utf-8"))
        if not (known and ok):
            return 401, {"error": "invalid_client", "error_description": "bad client credentials"}
        allowed = self.clients[client_id]
        requested = form.get("scope")
        scopes = tuple(requested.split()) if requested else tuple(allowed)
        if not scopes or any(s not in allowed for s in scopes):
            return 400, {"error": "invalid_scope", "error_description": "scope not permitted for this client"}
        ttl = self.default_ttl
        token = self.auth.mint(subject=client_id, scopes=scopes, ttl=ttl)
        return 200, {"access_token": token, "token_type": "Bearer", "expires_in": ttl, "scope": " ".join(scopes)}
