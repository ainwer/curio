"""Subprocess wrapper around git for the dedicated vault checkout."""

import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from curio_app.logging import redact

AUTHOR_NAME = "curio"
AUTHOR_EMAIL = "curio@localhost"

_DETAIL_LINES = 20
_REBASE_MARKERS = (".git/rebase-merge", ".git/rebase-apply")

_CONFLICT_MESSAGE = "The vault changed remotely in a way that conflicts with this save."
_PULL_FAILED_MESSAGE = "Could not update the vault from its remote."
_PUSH_REJECTED_MESSAGE = "The vault rejected the push."


class GitError(Exception):
    """Git operation failed; *detail* holds the redacted stderr for display."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class VaultConflict(GitError):
    """`git pull --rebase` failed (conflict or other rebase error)."""


class VaultDirty(GitError):
    """Working tree has uncommitted changes before a write."""


class VaultPushRejected(GitError):
    """Push rejected (non-fast-forward / remote refused)."""


def _redacted_tail(stderr: str, secrets: Sequence[str]) -> str:
    lines = stderr.splitlines()[-_DETAIL_LINES:]
    return "\n".join(redact(line, secrets) for line in lines)


def _verb(args: Sequence[str]) -> str:
    return args[0] if args else "git"


class GitRepo:
    """Runs git commands in a fixed checkout with pinned identity and settings."""

    def __init__(
        self,
        path: Path,
        *,
        branch: str,
        remote: str = "origin",
        ssh_key_file: Path | None = None,
        known_hosts_file: Path | None = None,
        secrets: Sequence[str] = (),
        timeout: float = 60.0,
    ) -> None:
        self.path = path
        self.branch = branch
        self.remote = remote
        self.ssh_key_file = ssh_key_file
        self.known_hosts_file = known_hosts_file
        self.secrets = tuple(secrets)
        self.timeout = timeout

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        if self.ssh_key_file is not None and self.ssh_key_file.exists():
            ssh_command = (
                f"ssh -i {shlex.quote(str(self.ssh_key_file))}"
                " -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes"
            )
            if self.known_hosts_file is not None:
                ssh_command += f" -o UserKnownHostsFile={shlex.quote(str(self.known_hosts_file))}"
            env["GIT_SSH_COMMAND"] = ssh_command
        return env

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run git in the checkout; on checked failure raise GitError with redacted stderr."""
        verb = _verb(args)
        command = [
            "git",
            "-C",
            str(self.path),
            "-c",
            f"user.name={AUTHOR_NAME}",
            "-c",
            f"user.email={AUTHOR_EMAIL}",
            "-c",
            "core.quotepath=off",
            *args,
        ]
        try:
            process = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {verb} timed out") from None
        if check and process.returncode != 0:
            raise GitError(f"git {verb} failed", _redacted_tail(process.stderr, self.secrets))
        return process

    def is_clean(self) -> bool:
        """Report whether the working tree has no uncommitted changes."""
        return self.run("status", "--porcelain").stdout.strip() == ""

    def head(self) -> str:
        """Return the full sha of HEAD."""
        return self.run("rev-parse", "HEAD").stdout.strip()

    def pull_rebase(self) -> None:
        """Fetch and rebase onto the remote branch; abort and raise VaultConflict on failure."""
        try:
            self.run("pull", "--rebase", "--no-autostash", self.remote, self.branch)
        except GitError as error:
            if self._rebase_in_progress():
                self.run("rebase", "--abort", check=False)
                raise VaultConflict(_CONFLICT_MESSAGE, error.detail) from error
            raise GitError(_PULL_FAILED_MESSAGE, error.detail) from error

    def commit_paths(self, paths: Sequence[str], message: str) -> str | None:
        """Commit only *paths*; return the new HEAD sha, or None when nothing changed."""
        self.run("add", "--", *paths)
        pending = self.run("diff", "--cached", "--quiet", "--", *paths, check=False)
        if pending.returncode == 0:
            return None
        self.run("commit", "-m", message, "--", *paths)
        return self.head()

    def push(self) -> None:
        """Push HEAD to the remote branch; raise VaultPushRejected when refused."""
        try:
            self.run("push", self.remote, f"HEAD:{self.branch}")
        except GitError as error:
            raise VaultPushRejected(_PUSH_REJECTED_MESSAGE, error.detail) from error

    def reset_hard(self, rev: str) -> None:
        """Reset the working tree to *rev* and drop untracked files."""
        self.run("reset", "--hard", rev)
        self.run("clean", "-fd")

    def _rebase_in_progress(self) -> bool:
        return any((self.path / marker).exists() for marker in _REBASE_MARKERS)
