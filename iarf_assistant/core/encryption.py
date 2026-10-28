"""Versioned authenticated encryption and installation-key lifecycle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from flask import current_app, has_app_context

from iarf_assistant.core.app_paths import AppPaths, restrict_secret_file

ENVELOPE_PREFIX = "enc:v2:"
ENVELOPE_VERSION = 2
ENVELOPE_ALGORITHM = "aesgcm"
KEY_FILE_FORMAT = "carlina.master-keys"
KEY_FILE_VERSION = 1
RECOVERY_FORMAT = "carlina.key-recovery"
RECOVERY_VERSION = 1
LOOKUP_DIGEST_VERSION = "lh1"


class EncryptionError(RuntimeError):
    """Base class for stable encryption failures callers must handle."""


class KeyUnavailable(EncryptionError):
    """Required persistent key material is absent or unknown."""


class NeedsUnlock(KeyUnavailable):
    """A legacy password-derived envelope needs an unlocked user session."""


class EncryptionIntegrityError(EncryptionError):
    """An authenticated envelope or recovery bundle failed validation."""


class KeyStoreFormatError(EncryptionError):
    """Persistent key material is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class MasterKey:
    key_id: str
    material: bytes
    created_at: str


class LegacyUnlockRegistry:
    """Process-local bridge for legacy user envelopes during migration.

    Flask's default session is a signed client-side cookie, so raw derived
    encryption keys must never be placed in it. The cookie carries only an
    opaque, expiring handle; the key remains in the managed single runtime.
    """

    def __init__(self, *, ttl_seconds: int = 12 * 60 * 60, maximum_entries: int = 512):
        self.ttl_seconds = int(ttl_seconds)
        self.maximum_entries = int(maximum_entries)
        self._entries: dict[str, tuple[bytes, int, float]] = {}
        self._lock = threading.RLock()

    def issue(self, key: str | bytes, user_id: int) -> str:
        material = _key_bytes(key)
        with self._lock:
            self._purge_expired()
            if len(self._entries) >= self.maximum_entries:
                oldest = min(self._entries, key=lambda token: self._entries[token][2])
                self._entries.pop(oldest, None)
            token = f"unlock:{secrets.token_urlsafe(32)}"
            self._entries[token] = (
                material,
                int(user_id),
                time.monotonic() + self.ttl_seconds,
            )
            return token

    def resolve(self, token: str | None, user_id: int | None = None) -> bytes | None:
        if not token or not token.startswith("unlock:"):
            return None
        with self._lock:
            self._purge_expired()
            record = self._entries.get(token)
            if record is None:
                return None
            key, owner_id, _expires_at = record
            if user_id is not None and owner_id != int(user_id):
                return None
            return key

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._entries.pop(token, None)

    def _purge_expired(self) -> None:
        now = time.monotonic()
        for token, (_key, _owner_id, expires_at) in tuple(self._entries.items()):
            if expires_at <= now:
                self._entries.pop(token, None)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64decode(value: str, *, label: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise KeyStoreFormatError(f"Invalid base64 {label}.") from exc


def _key_id(material: bytes) -> str:
    return f"mk-{hashlib.sha256(material).hexdigest()[:16]}"


def _atomic_secret_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        restrict_secret_file(temporary)
        os.replace(temporary, path)
        restrict_secret_file(path)
    finally:
        temporary.unlink(missing_ok=True)


class InstallationKeyStore:
    """Permission-restricted file-backed installation master-key registry."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        metadata_path: str | os.PathLike[str] | None = None,
    ):
        self.path = Path(path).expanduser().resolve()
        self.metadata_path = (
            Path(
                metadata_path or self.path.with_name(f"{self.path.stem}.metadata.json")
            )
            .expanduser()
            .resolve()
        )
        self._lock = threading.RLock()
        self._active_key_id: str | None = None
        self._keys: dict[str, MasterKey] = {}

    @classmethod
    def ephemeral(cls) -> InstallationKeyStore:
        store = cls(Path.cwd() / ".carlina-ephemeral-key-store")
        material = AESGCM.generate_key(bit_length=256)
        key = MasterKey(_key_id(material), material, _utc_now())
        store._keys[key.key_id] = key
        store._active_key_id = key.key_id
        return store

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def initialize(self) -> str:
        """Create an initial key once, or return the existing active key ID."""

        with self._lock:
            if self.exists:
                self.load()
                return self.active_key_id
            if self.metadata_path.exists():
                raise KeyUnavailable(
                    "Installation key metadata exists but the secret key file is "
                    f"missing: {self.path}. Restore recovery material before startup."
                )
            material = AESGCM.generate_key(bit_length=256)
            key = MasterKey(_key_id(material), material, _utc_now())
            self._keys = {key.key_id: key}
            self._active_key_id = key.key_id
            self._persist()
            return key.key_id

    def load(self) -> None:
        with self._lock:
            if not self.exists:
                raise KeyUnavailable(
                    f"Installation master-key file is missing: {self.path}"
                )
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise KeyStoreFormatError(
                    f"Could not read installation master keys: {self.path}"
                ) from exc
            if not isinstance(payload, dict):
                raise KeyStoreFormatError("Master-key file must contain an object.")
            if payload.get("format") != KEY_FILE_FORMAT:
                raise KeyStoreFormatError("Unsupported master-key file format.")
            if payload.get("version") != KEY_FILE_VERSION:
                raise KeyStoreFormatError("Unsupported master-key file version.")
            raw_keys = payload.get("keys")
            active_key_id = payload.get("active_key_id")
            if not isinstance(raw_keys, dict) or not isinstance(active_key_id, str):
                raise KeyStoreFormatError("Master-key registry is incomplete.")

            parsed: dict[str, MasterKey] = {}
            for key_id, record in raw_keys.items():
                if not isinstance(key_id, str) or not isinstance(record, dict):
                    raise KeyStoreFormatError("Master-key record is invalid.")
                if record.get("algorithm") != ENVELOPE_ALGORITHM:
                    raise KeyStoreFormatError(
                        f"Unsupported algorithm for key {key_id}."
                    )
                material_value = record.get("material")
                created_at = record.get("created_at")
                if not isinstance(material_value, str) or not isinstance(
                    created_at, str
                ):
                    raise KeyStoreFormatError(
                        f"Master-key record {key_id} is incomplete."
                    )
                material = _b64decode(material_value, label=f"key {key_id}")
                if len(material) != 32 or _key_id(material) != key_id:
                    raise KeyStoreFormatError(
                        f"Master-key fingerprint mismatch for {key_id}."
                    )
                parsed[key_id] = MasterKey(key_id, material, created_at)

            if active_key_id not in parsed:
                raise KeyStoreFormatError("Active master key is not present.")
            self._keys = parsed
            self._active_key_id = active_key_id
            restrict_secret_file(self.path)

    @property
    def active_key_id(self) -> str:
        if not self._active_key_id:
            self.load()
        if not self._active_key_id:
            raise KeyUnavailable("No active installation master key is configured.")
        return self._active_key_id

    def active_key(self) -> MasterKey:
        return self.key(self.active_key_id)

    def key(self, key_id: str) -> MasterKey:
        if not self._keys:
            self.load()
        try:
            return self._keys[key_id]
        except KeyError as exc:
            raise KeyUnavailable(
                f"Encryption key {key_id!r} is not available in this installation."
            ) from exc

    def key_ids(self) -> tuple[str, ...]:
        if not self._keys:
            self.load()
        return tuple(sorted(self._keys))

    def rotate(self) -> str:
        """Activate a new key while retaining old keys for existing envelopes."""

        with self._lock:
            if self.exists:
                self.load()
            elif not self._keys:
                raise KeyUnavailable("Initialize the key store before rotating it.")
            material = AESGCM.generate_key(bit_length=256)
            key = MasterKey(_key_id(material), material, _utc_now())
            self._keys[key.key_id] = key
            self._active_key_id = key.key_id
            self._persist()
            return key.key_id

    def public_metadata(self) -> dict[str, Any]:
        if not self._keys:
            self.load()
        return {
            "format": KEY_FILE_FORMAT,
            "version": KEY_FILE_VERSION,
            "backend": "restricted-file",
            "active_key_id": self.active_key_id,
            "keys": [
                {
                    "key_id": key.key_id,
                    "algorithm": ENVELOPE_ALGORITHM,
                    "created_at": key.created_at,
                    "active": key.key_id == self.active_key_id,
                }
                for key in sorted(self._keys.values(), key=lambda item: item.key_id)
            ],
        }

    def export_recovery_bundle(
        self,
        passphrase: str,
        *,
        session_key: str | None = None,
    ) -> bytes:
        """Export opt-in recovery material encrypted by an operator passphrase."""

        if len(passphrase) < 12:
            raise ValueError("Recovery passphrase must contain at least 12 characters.")
        if not self._keys:
            self.load()
        inner = {
            "master_keys": self._payload(),
            "session_key": session_key,
            "exported_at": _utc_now(),
        }
        plaintext = json.dumps(inner, sort_keys=True).encode("utf-8")
        salt = os.urandom(16)
        nonce = os.urandom(12)
        derived = _recovery_key(passphrase, salt)
        aad = f"{RECOVERY_FORMAT}:{RECOVERY_VERSION}".encode("ascii")
        ciphertext = AESGCM(derived).encrypt(nonce, plaintext, aad)
        outer = {
            "format": RECOVERY_FORMAT,
            "version": RECOVERY_VERSION,
            "kdf": {"name": "scrypt", "n": 32768, "r": 8, "p": 1},
            "salt": _b64encode(salt),
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(ciphertext),
        }
        return (json.dumps(outer, indent=2, sort_keys=True) + "\n").encode("utf-8")

    @staticmethod
    def decrypt_recovery_bundle(bundle: bytes | str, passphrase: str) -> dict[str, Any]:
        try:
            raw = bundle.decode("utf-8") if isinstance(bundle, bytes) else bundle
            outer = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KeyStoreFormatError("Recovery bundle is not valid JSON.") from exc
        if not isinstance(outer, dict) or outer.get("format") != RECOVERY_FORMAT:
            raise KeyStoreFormatError("Unsupported recovery bundle format.")
        if outer.get("version") != RECOVERY_VERSION:
            raise KeyStoreFormatError("Unsupported recovery bundle version.")
        try:
            salt = _b64decode(str(outer["salt"]), label="recovery salt")
            nonce = _b64decode(str(outer["nonce"]), label="recovery nonce")
            ciphertext = _b64decode(
                str(outer["ciphertext"]), label="recovery ciphertext"
            )
        except KeyError as exc:
            raise KeyStoreFormatError("Recovery bundle is incomplete.") from exc
        aad = f"{RECOVERY_FORMAT}:{RECOVERY_VERSION}".encode("ascii")
        try:
            plaintext = AESGCM(_recovery_key(passphrase, salt)).decrypt(
                nonce,
                ciphertext,
                aad,
            )
            payload = json.loads(plaintext)
        except (InvalidTag, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EncryptionIntegrityError(
                "Recovery passphrase is incorrect or the bundle was modified."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("master_keys"), dict
        ):
            raise KeyStoreFormatError("Recovery bundle payload is incomplete.")
        return payload

    def restore_recovery_bundle(
        self,
        bundle: bytes | str,
        passphrase: str,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self.exists and not replace:
                raise FileExistsError(
                    "Master-key file already exists; pass replace=True explicitly."
                )
            payload = self.decrypt_recovery_bundle(bundle, passphrase)
            key_payload = payload["master_keys"]
            _atomic_secret_write(
                self.path,
                (json.dumps(key_payload, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
            self._keys = {}
            self._active_key_id = None
            self.load()
            self._persist_metadata()
            return payload

    def _payload(self) -> dict[str, Any]:
        return {
            "format": KEY_FILE_FORMAT,
            "version": KEY_FILE_VERSION,
            "active_key_id": self.active_key_id,
            "keys": {
                key_id: {
                    "algorithm": ENVELOPE_ALGORITHM,
                    "created_at": key.created_at,
                    "material": _b64encode(key.material),
                }
                for key_id, key in sorted(self._keys.items())
            },
        }

    def _persist(self) -> None:
        _atomic_secret_write(
            self.path,
            (json.dumps(self._payload(), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )
        self._persist_metadata()

    def _persist_metadata(self) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_path.with_name(
            f".{self.metadata_path.name}.tmp-{os.getpid()}"
        )
        temporary.write_text(
            json.dumps(self.public_metadata(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.metadata_path)


def _recovery_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(
        passphrase.encode("utf-8")
    )


class EncryptionService:
    """Encrypt v2 values and read both strict v2 and documented legacy forms."""

    def __init__(self, key_store: InstallationKeyStore):
        self.key_store = key_store

    @classmethod
    def ephemeral(cls) -> EncryptionService:
        return cls(InstallationKeyStore.ephemeral())

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("Encrypted plaintext must be a string.")
        key = self.key_store.active_key()
        header = f"enc:v{ENVELOPE_VERSION}:{key.key_id}:{ENVELOPE_ALGORITHM}"
        nonce = os.urandom(12)
        ciphertext = AESGCM(key.material).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            header.encode("ascii"),
        )
        return f"{header}:{_b64encode(nonce + ciphertext)}"

    def decrypt(
        self,
        value: str,
        *,
        legacy_system_secret: str | bytes | None = None,
        legacy_user_key: str | bytes | None = None,
        legacy_strategy: str = "user",
    ) -> str:
        if not isinstance(value, str):
            raise TypeError("Encrypted value must be a string.")
        if value.startswith("enc:"):
            return self._decrypt_versioned(value)
        if value.startswith("sys:"):
            if not legacy_system_secret:
                raise KeyUnavailable(
                    "A legacy system envelope requires the former Flask secret key."
                )
            return _legacy_fernet_decrypt(
                value[4:],
                _legacy_system_key(legacy_system_secret),
                label="legacy system envelope",
            )
        if value.startswith("usr:"):
            if not legacy_user_key:
                raise NeedsUnlock(
                    "A legacy user envelope requires the user's unlocked password key."
                )
            return _legacy_fernet_decrypt(
                value[4:],
                legacy_user_key,
                label="legacy user envelope",
            )

        # Prefixless legacy rows may be either plaintext or Fernet tokens.  Try
        # only keys that are actually available, then preserve known plaintext.
        candidates: list[str | bytes] = []
        if legacy_strategy == "system" and legacy_system_secret:
            candidates.append(_legacy_system_key(legacy_system_secret))
        if legacy_user_key:
            candidates.append(legacy_user_key)
        if legacy_strategy != "system" and legacy_system_secret:
            candidates.append(_legacy_system_key(legacy_system_secret))
        for candidate in candidates:
            try:
                return (
                    Fernet(_key_bytes(candidate))
                    .decrypt(value.encode("utf-8"))
                    .decode("utf-8", errors="replace")
                )
            except (InvalidToken, ValueError):
                continue
        return value

    def _decrypt_versioned(self, value: str) -> str:
        parts = value.split(":", 4)
        if len(parts) != 5:
            raise EncryptionIntegrityError("Encrypted envelope header is malformed.")
        prefix, version, key_id, algorithm, encoded = parts
        if prefix != "enc" or version != f"v{ENVELOPE_VERSION}":
            raise EncryptionIntegrityError("Encrypted envelope version is unsupported.")
        if algorithm != ENVELOPE_ALGORITHM:
            raise EncryptionIntegrityError(
                "Encrypted envelope algorithm is unsupported."
            )
        payload = _b64decode(encoded, label="encrypted envelope")
        # AES-GCM permits empty plaintext: 12 nonce bytes plus the 16-byte
        # authentication tag is therefore the smallest valid envelope.
        if len(payload) < 28:
            raise EncryptionIntegrityError("Encrypted envelope payload is truncated.")
        nonce, ciphertext = payload[:12], payload[12:]
        key = self.key_store.key(key_id)
        header = f"{prefix}:{version}:{key_id}:{algorithm}".encode("ascii")
        try:
            return (
                AESGCM(key.material)
                .decrypt(nonce, ciphertext, header)
                .decode(
                    "utf-8",
                    errors="strict",
                )
            )
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise EncryptionIntegrityError(
                "Encrypted value failed authentication."
            ) from exc


def _key_bytes(value: str | bytes) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _legacy_system_key(secret: str | bytes) -> bytes:
    secret_bytes = _key_bytes(secret)
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"system_static_salt_iarf",
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret_bytes))


def derive_legacy_system_key(secret: str | bytes) -> bytes:
    """Derive the documented pre-v2 installation Fernet key."""

    return _legacy_system_key(secret)


def derive_legacy_user_key(password: str, password_hash: str) -> bytes:
    """Derive the documented pre-v2 per-user Fernet key."""

    if not password or not password_hash:
        raise ValueError("A password and stored password hash are required.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=password_hash.encode("utf-8"),
        iterations=480000,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _legacy_fernet_decrypt(
    token: str,
    key: str | bytes,
    *,
    label: str,
) -> str:
    try:
        return (
            Fernet(_key_bytes(key))
            .decrypt(token.encode("utf-8"))
            .decode("utf-8", errors="replace")
        )
    except (InvalidToken, ValueError) as exc:
        raise EncryptionIntegrityError(f"Could not authenticate {label}.") from exc


def configure_encryption(
    app,
    paths: AppPaths,
    *,
    initialize: bool = False,
) -> EncryptionService:
    store = InstallationKeyStore(paths.master_keys_file)
    if initialize:
        store.initialize()
    else:
        store.load()
    service = EncryptionService(store)
    app.extensions["carlina_encryption"] = service
    app.extensions.setdefault("carlina_legacy_unlocks", LegacyUnlockRegistry())
    return service


def encryption_service_from_context() -> EncryptionService:
    if not has_app_context():
        raise KeyUnavailable("Encryption requires a Carlina application context.")
    service = current_app.extensions.get("carlina_encryption")
    if isinstance(service, EncryptionService):
        return service
    if current_app.testing:
        service = EncryptionService.ephemeral()
        current_app.extensions["carlina_encryption"] = service
        return service
    raise KeyUnavailable(
        "Persistent installation encryption has not been configured for this app."
    )


def keyed_lookup_digest(
    value: str,
    *,
    purpose: str,
    key_id: str | None = None,
) -> str:
    """Return a versioned, installation-keyed digest for an encrypted value."""
    if not isinstance(value, str):
        raise TypeError("Lookup digest values must be strings.")
    normalized_purpose = str(purpose or "").strip().casefold()
    if not normalized_purpose:
        raise ValueError("Lookup digest purpose is required.")
    service = encryption_service_from_context()
    selected_key = (
        service.key_store.key(key_id)
        if key_id
        else service.key_store.active_key()
    )
    message = (
        f"carlina:{LOOKUP_DIGEST_VERSION}:{normalized_purpose}\0{value}"
    ).encode("utf-8")
    digest = hmac.new(selected_key.material, message, hashlib.sha256).hexdigest()
    return f"{LOOKUP_DIGEST_VERSION}:{selected_key.key_id}:{digest}"


def keyed_lookup_candidates(value: str, *, purpose: str) -> tuple[str, ...]:
    """Return digests for every retained installation key, active key first."""
    service = encryption_service_from_context()
    active_key_id = service.key_store.active_key_id
    key_ids = (
        active_key_id,
        *(key_id for key_id in service.key_store.key_ids() if key_id != active_key_id),
    )
    return tuple(
        keyed_lookup_digest(value, purpose=purpose, key_id=key_id)
        for key_id in key_ids
    )


def ensure_session_key(path: str | os.PathLike[str]) -> str:
    session_path = Path(path).expanduser().resolve()
    if session_path.exists():
        key = session_path.read_text(encoding="utf-8").strip()
        if len(key) < 32:
            raise KeyStoreFormatError("Persistent Flask session key is invalid.")
        restrict_secret_file(session_path)
        return key
    key = secrets.token_urlsafe(48)
    _atomic_secret_write(session_path, f"{key}\n".encode())
    return key


def load_session_key(path: str | os.PathLike[str]) -> str:
    """Load an initialized Flask signing key without creating state."""

    session_path = Path(path).expanduser().resolve()
    try:
        key = session_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise KeyUnavailable(
            "Persistent Flask session key is missing. Run 'carlina init'."
        ) from exc
    if len(key) < 32:
        raise KeyStoreFormatError("Persistent Flask session key is invalid.")
    restrict_secret_file(session_path)
    return key


def store_legacy_user_key(key: str | bytes, user_id: int) -> str:
    """Store a legacy unlock key server-side and return an opaque handle."""

    if not has_app_context():
        raise KeyUnavailable("A Carlina application context is required.")
    registry = current_app.extensions.get("carlina_legacy_unlocks")
    if not isinstance(registry, LegacyUnlockRegistry):
        registry = LegacyUnlockRegistry()
        current_app.extensions["carlina_legacy_unlocks"] = registry
    return registry.issue(key, user_id)


def resolve_legacy_user_key(token: str | None, user_id: int | None = None) -> bytes | None:
    """Resolve an opaque legacy unlock handle within the managed process."""

    if not token or not has_app_context():
        return None
    registry = current_app.extensions.get("carlina_legacy_unlocks")
    if isinstance(registry, LegacyUnlockRegistry):
        resolved = registry.resolve(token, user_id)
        if resolved is not None:
            return resolved
    # Tests written against the historical session contract can continue to
    # construct legacy envelopes without making raw-key cookies a production
    # compatibility feature.
    if current_app.testing and not token.startswith("unlock:"):
        return token.encode("utf-8")
    return None


def revoke_legacy_user_key(token: str | None) -> None:
    if not token or not has_app_context():
        return
    registry = current_app.extensions.get("carlina_legacy_unlocks")
    if isinstance(registry, LegacyUnlockRegistry):
        registry.revoke(token)


def envelope_key_id(value: str) -> str | None:
    if not isinstance(value, str) or not value.startswith(ENVELOPE_PREFIX):
        return None
    parts = value.split(":", 4)
    if len(parts) != 5 or parts[3] != ENVELOPE_ALGORITHM:
        return None
    return parts[2]


__all__ = [
    "ENVELOPE_ALGORITHM",
    "ENVELOPE_PREFIX",
    "ENVELOPE_VERSION",
    "EncryptionError",
    "EncryptionIntegrityError",
    "EncryptionService",
    "InstallationKeyStore",
    "KeyStoreFormatError",
    "KeyUnavailable",
    "MasterKey",
    "LegacyUnlockRegistry",
    "LOOKUP_DIGEST_VERSION",
    "NeedsUnlock",
    "configure_encryption",
    "derive_legacy_system_key",
    "derive_legacy_user_key",
    "encryption_service_from_context",
    "ensure_session_key",
    "load_session_key",
    "keyed_lookup_candidates",
    "keyed_lookup_digest",
    "envelope_key_id",
    "resolve_legacy_user_key",
    "revoke_legacy_user_key",
    "store_legacy_user_key",
]
