from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet

from app.core.config import get_settings


@lru_cache
def _fernet() -> MultiFernet:
    keys = get_settings().fernet_keys.get_secret_value().split(",")
    return MultiFernet([Fernet(k.strip()) for k in keys if k.strip()])


def encrypt(plaintext: str) -> bytes:
    """Encrypt a credential with the primary (first) key. Store the result in a bytea column."""
    return _fernet().encrypt(plaintext.encode())


def decrypt(token: bytes) -> str:
    """Decrypt with any configured key; raises cryptography.fernet.InvalidToken if none match."""
    return _fernet().decrypt(token).decode()
