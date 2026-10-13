"""Lightweight authentication — password hashing + signed session cookie.

Stdlib only (no extra deps): PBKDF2-HMAC-SHA256 for passwords, and an
HMAC-signed stateless cookie for sessions. Good enough for a demo; for production
rotate the secret, add expiry, and serve over HTTPS (Cloud Run does).
"""

import base64
import hashlib
import hmac
import os

COOKIE_NAME = "vm_session"
_SECRET = os.environ.get("SESSION_SECRET", "dev-insecure-secret-change-me")


# --- passwords ------------------------------------------------------------- #

def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# --- session cookie -------------------------------------------------------- #

def make_token(email: str) -> str:
    sig = hmac.new(_SECRET.encode(), email.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{email}|{sig}".encode()).decode()


def read_token(token: str) -> str | None:
    """Returns the email if the token's signature is valid, else None."""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        email, sig = raw.rsplit("|", 1)
        good = hmac.new(_SECRET.encode(), email.encode(), hashlib.sha256).hexdigest()
        return email if hmac.compare_digest(sig, good) else None
    except Exception:
        return None


def public_user(user: dict) -> dict:
    """Strips the password hash before returning a user to the client."""
    return {k: v for k, v in user.items() if k != "password_hash"}
