import hashlib
from passlib.context import CryptContext
from passlib.exc import UnknownHashError

# Prefer a modern, length-agnostic hash for new passwords.
# Keep bcrypt-based schemes available so existing hashes continue to verify.
_pwd_context = CryptContext(
    schemes=["pbkdf2_sha256", "bcrypt_sha256", "bcrypt"],
    deprecated="auto",
    bcrypt_sha256__truncate_error=False,
    bcrypt__truncate_error=False,
)


def _normalize_password(password: str) -> str:
    """Return a representation that never exceeds bcrypt's 72-byte limit."""
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        return hashlib.sha256(encoded).hexdigest()
    return password


def hash_password(password: str) -> str:
    """Hash the password using the active scheme while handling long inputs."""
    normalized = _normalize_password(password)
    try:
        return _pwd_context.hash(normalized)
    except ValueError as exc:
        # Handle third-party libraries that still enforce 72-byte limits.
        if "72 bytes" in str(exc):
            digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
            return _pwd_context.hash(digest)
        raise


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify `plain_password` against a stored hash, accommodating legacy formats."""
    normalized = _normalize_password(plain_password)
    try:
        if _pwd_context.verify(normalized, hashed_password):
            return True
    except UnknownHashError:
        # Some legacy hashes may not include scheme metadata; attempt bcrypt directly.
        pass
    except ValueError as exc:
        if "72 bytes" not in str(exc):
            raise

    digest = hashlib.sha256(plain_password.encode("utf-8")).hexdigest()
    try:
        return _pwd_context.verify(digest, hashed_password)
    except UnknownHashError:
        return False
