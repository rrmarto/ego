from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from pathlib import Path

from ego.config import AppPaths


class ServiceCredentialError(RuntimeError):
    pass


class ServiceCredentialStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def get_or_create(self) -> str:
        self.paths.ensure()
        path = self.paths.service_token_file
        if path.exists() or path.is_symlink():
            return self._read(path)
        token = self._new_token()
        try:
            self._create(path, token)
        except FileExistsError:
            return self._read(path)
        return token

    def regenerate(self) -> str:
        self.paths.ensure()
        token = self._new_token()
        target = self.paths.service_token_file
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._create(temporary, token)
            os.replace(temporary, target)
            target.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
        return token

    @staticmethod
    def matches(expected: str, supplied: str) -> bool:
        return secrets.compare_digest(expected, supplied)

    @staticmethod
    def new_nonce() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def server_proof(token: str, nonce: str) -> str:
        return ServiceCredentialStore._proof(token, f"server:{nonce}")

    @staticmethod
    def client_proof(
        token: str,
        nonce: str,
        protocol_version: int,
        request_id: str,
        method: str,
    ) -> str:
        return ServiceCredentialStore._proof(
            token,
            f"client:{nonce}:{protocol_version}:{request_id}:{method}",
        )

    @staticmethod
    def _proof(token: str, message: str) -> str:
        return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _new_token() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _create(path: Path, token: str) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{token}\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _read(path: Path) -> str:
        if path.is_symlink() or not path.is_file():
            raise ServiceCredentialError("service credential path is not a regular file")
        stat = path.stat()
        if stat.st_uid != os.getuid():
            raise ServiceCredentialError("service credential is not owned by the current user")
        if stat.st_mode & 0o077:
            path.chmod(0o600)
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ServiceCredentialError("service credential is empty; regenerate it explicitly")
        return token
