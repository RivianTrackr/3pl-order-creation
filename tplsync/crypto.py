"""Encryption for secrets stored in SQLite (API keys, client secrets)."""

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("Stored secret can't be decrypted - was TPLSYNC_ENCRYPTION_KEY changed?") from exc


def generate_key() -> str:
    return Fernet.generate_key().decode()
