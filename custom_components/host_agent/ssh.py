"""SSH transport (asyncssh) with a pinned host key."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from dataclasses import dataclass
from types import TracebackType

import asyncssh

from .const import COMMAND_TIMEOUT, SSH_CONNECT_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class SshError(Exception):
    """Base class of SSH errors."""


class CannotConnect(SshError):
    """Host unreachable or SSH failure."""


class InvalidAuth(SshError):
    """Authentication refused."""


class InvalidKey(SshError):
    """The provided private key cannot be parsed."""


class HostKeyMismatch(SshError):
    """The server host key differs from the pinned one."""

    def __init__(self, fingerprint: str | None = None) -> None:
        super().__init__("host key mismatch")
        self.fingerprint = fingerprint


class CommandError(SshError):
    """A remote command returned a non-zero exit status."""

    def __init__(self, command: str, returncode: int, stdout: str, stderr: str) -> None:
        detail = (stderr or stdout).strip().splitlines()
        super().__init__(f"{command.split()[0]} failed ({returncode}): {detail[-1] if detail else ''}")
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass(frozen=True)
class HostKey:
    public_key: str  # OpenSSH format
    fingerprint: str  # SHA256:...


@dataclass(frozen=True)
class GeneratedKey:
    private_key: str
    public_key: str


def generate_client_key(comment: str) -> GeneratedKey:
    """Generate an ed25519 key pair for Home Assistant. Fast, but CPU bound: run in an executor."""
    key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
    return GeneratedKey(
        private_key=key.export_private_key().decode(),
        public_key=key.export_public_key().decode().strip(),
    )


def fingerprint_of(public_key: str) -> str:
    return asyncssh.import_public_key(public_key).get_fingerprint("sha256")


class SshClient:
    """Async context manager wrapping one SSH connection.

    `host_key=None` accepts any key (trust on first use): the key is then
    available in `server_host_key` and must be confirmed by the user.
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        *,
        password: str | None = None,
        private_key: str | None = None,
        host_key: str | None = None,
        connect_timeout: float = SSH_CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self._private_key = private_key
        self._host_key = host_key
        self._connect_timeout = connect_timeout
        self._conn: asyncssh.SSHClientConnection | None = None
        self.server_host_key: HostKey | None = None

    async def __aenter__(self) -> SshClient:
        await self.connect()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        client_keys: list[asyncssh.SSHKey] = []
        if self._private_key:
            try:
                client_keys = [asyncssh.import_private_key(self._private_key)]
            except (asyncssh.KeyImportError, ValueError) as err:
                raise InvalidKey(str(err)) from err

        known_hosts: object = None
        if self._host_key:
            known_hosts = ([asyncssh.import_public_key(self._host_key)], [], [])
        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(
                    self.host,
                    port=self.port,
                    username=self.username,
                    password=self._password,
                    client_keys=client_keys,
                    known_hosts=known_hosts,
                    agent_path=None,
                    config=None,
                    preferred_auth="publickey,password,keyboard-interactive"
                    if client_keys
                    else "password,keyboard-interactive",
                    keepalive_interval=30,
                ),
                timeout=self._connect_timeout,
            )
        except asyncssh.HostKeyNotVerifiable as err:
            raise HostKeyMismatch() from err
        except asyncssh.PermissionDenied as err:
            raise InvalidAuth(str(err)) from err
        except (TimeoutError, OSError, asyncssh.Error) as err:
            raise CannotConnect(str(err) or type(err).__name__) from err

        key = self._conn.get_server_host_key()
        if key is not None:
            self.server_host_key = HostKey(
                public_key=key.export_public_key().decode().strip(),
                fingerprint=key.get_fingerprint("sha256"),
            )

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._conn.wait_closed(), timeout=5)
            self._conn = None

    @property
    def conn(self) -> asyncssh.SSHClientConnection:
        if self._conn is None:
            raise CannotConnect("not connected")
        return self._conn

    async def run(
        self,
        command: str,
        *,
        stdin: str | None = None,
        check: bool = True,
        timeout: float = COMMAND_TIMEOUT,
    ) -> asyncssh.SSHCompletedProcess:
        try:
            result = await asyncio.wait_for(self.conn.run(command, input=stdin, check=False), timeout=timeout)
        except TimeoutError as err:
            raise CannotConnect(f"timeout running {command.split()[0]}") from err
        except (OSError, asyncssh.Error) as err:
            raise CannotConnect(str(err)) from err
        rc = result.exit_status if result.exit_status is not None else 255
        if check and rc != 0:
            raise CommandError(command, rc, str(result.stdout or ""), str(result.stderr or ""))
        return result

    async def put(self, data: bytes, remote_path: str, mode: int = 0o600) -> None:
        """Write `data` to `remote_path` atomically (temporary file + rename)."""
        tmp = f"{remote_path}.part"
        try:
            async with self.conn.start_sftp_client() as sftp:
                async with sftp.open(tmp, "wb") as fh:
                    await fh.write(data)
                await sftp.chmod(tmp, mode)
                await sftp.posix_rename(tmp, remote_path)
        except (OSError, asyncssh.Error) as err:
            raise CannotConnect(f"upload of {remote_path} failed: {err}") from err


def quote(value: str) -> str:
    return shlex.quote(value)
