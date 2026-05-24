from __future__ import annotations

import re
from pathlib import Path


PRIVATE_KEY_HEX_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
SECP256K1_ORDER = int(
    "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141",
    16,
)


class FilePrivateKeyProvider:
    def read_private_key(self, file_path: str) -> str:
        path = Path(file_path)
        private_key = path.read_text(encoding="utf-8").strip()
        validate_private_key(private_key)
        return private_key


def validate_private_key(private_key: str) -> None:
    normalized = private_key[2:] if private_key.lower().startswith("0x") else private_key
    if not normalized:
        raise ValueError("private key file is empty")
    if not PRIVATE_KEY_HEX_PATTERN.fullmatch(normalized):
        raise ValueError("private key must be 32-byte hex")
    scalar = int(normalized, 16)
    if not 0 < scalar < SECP256K1_ORDER:
        raise ValueError("private key must be in the secp256k1 scalar range")
