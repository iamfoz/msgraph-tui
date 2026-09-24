"""Per-approver Ed25519 signatures for four-eyes approvals.

A shared HMAC key proves only that *someone holding the key* approved. With
Ed25519 each approver holds their own private key and the organisation keeps a
**trust store** mapping approver identities to public keys. An approval then
verifies only if it was signed by a key registered to the named approver, so a
requester cannot forge a colleague's approval without that colleague's key.

- Private keys are generated locally (``graphdeck keygen``), written with 0600
  permissions, optionally passphrase-encrypted, and never logged, audited or
  copied into the state directory.
- The trust store is a small JSON file the organisation controls::

      {"approvers": {"bob@contoso.example": ["<base64 public key>", ...]}}

  Keep it somewhere requesters cannot edit (config management, a protected
  repo path, read-only mount): whoever can add a key to it can approve.

``cryptography`` is an optional dependency (``pip install msgraph-tui[signing]``,
also pulled in by the ``live`` extra via MSAL); it is imported lazily.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ED25519 = "ed25519"
HMAC_SHA256 = "hmac-sha256"


class SigningUnavailable(RuntimeError):
    """The optional ``cryptography`` package is not installed."""


def _crypto() -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SigningUnavailable(
            "Ed25519 approval signing needs the 'cryptography' package: "
            "pip install 'msgraph-tui[signing]'"
        ) from exc
    return serialization, ed25519


def key_id(public_b64: str) -> str:
    """Short, stable fingerprint of a public key (shown to humans, stored on approvals)."""
    return hashlib.sha256(base64.b64decode(public_b64)).hexdigest()[:16]


def generate_keypair(passphrase: bytes | None = None) -> tuple[bytes, str]:
    """Return ``(private_key_pem, public_key_b64)``."""
    serialization, ed25519 = _crypto()
    private = ed25519.Ed25519PrivateKey.generate()
    encryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase else serialization.NoEncryption()
    )
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption
    )
    return pem, _public_b64(private)


def _public_b64(private: Any) -> str:
    serialization, _ = _crypto()
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def write_private_key(path: Path, pem: bytes) -> None:
    """Create the key file with owner-only permissions; never overwrite."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)


@dataclass
class ApproverKey:
    """A loaded private key. Keep in memory only; ``repr`` never shows key material."""

    _private: Any = field(repr=False)
    public_b64: str = ""

    @property
    def key_id(self) -> str:
        return key_id(self.public_b64)

    def sign(self, message: bytes) -> str:
        return base64.b64encode(self._private.sign(message)).decode("ascii")


def load_private_key(path: Path, passphrase: bytes | None = None) -> ApproverKey:
    serialization, ed25519 = _crypto()
    data = Path(path).expanduser().read_bytes()
    try:
        private = serialization.load_pem_private_key(data, password=passphrase)
    except TypeError as exc:
        raise ValueError(
            "Approver key is passphrase-protected: set GRAPHDECK_APPROVER_KEY_PASSPHRASE "
            "or enter it when prompted." if passphrase is None
            else "Approver key is not encrypted; do not supply a passphrase."
        ) from exc
    except ValueError as exc:
        raise ValueError(f"Could not load approver key {path}: wrong passphrase or not a PEM key.") from exc
    if not isinstance(private, ed25519.Ed25519PrivateKey):
        raise ValueError(f"Approver key {path} is not an Ed25519 key.")
    return ApproverKey(private, _public_b64(private))


def verify_signature(public_b64: str, message: bytes, signature_b64: str) -> bool:
    _, ed25519 = _crypto()
    try:
        public = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        public.verify(base64.b64decode(signature_b64), message)
    except Exception:  # InvalidSignature, bad base64, wrong length
        return False
    return True


class TrustStore:
    """Approver identity → registered public keys (identities compared case-insensitively)."""

    def __init__(self, approvers: dict[str, list[str]] | None = None) -> None:
        self._keys: dict[str, list[str]] = {}
        for identity, keys in (approvers or {}).items():
            self._keys.setdefault(identity.strip().lower(), []).extend(keys)

    @classmethod
    def load(cls, path: Path) -> TrustStore:
        try:
            data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read approver trust store {path}: {exc}") from exc
        approvers = data.get("approvers") if isinstance(data, dict) else None
        if not isinstance(approvers, dict) or not all(
            isinstance(v, list) and all(isinstance(k, str) for k in v) for v in approvers.values()
        ):
            raise ValueError(
                f"Approver trust store {path} must look like "
                '{"approvers": {"name@example.com": ["<base64 public key>"]}}'
            )
        return cls(approvers)

    def keys_for(self, identity: str) -> list[str]:
        return list(self._keys.get(identity.strip().lower(), []))

    def find_key(self, identity: str, kid: str | None) -> str | None:
        for public_b64 in self.keys_for(identity):
            if kid is None or key_id(public_b64) == kid:
                return public_b64
        return None

    def identities(self) -> list[str]:
        return sorted(self._keys)
