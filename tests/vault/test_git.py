"""Tests for vault.git.GitRepo against a temporary remote and two clones."""

import shlex
import subprocess
from pathlib import Path

import gitutil
import pytest

from curio_app.vault.git import (
    AUTHOR_EMAIL,
    AUTHOR_NAME,
    GitError,
    GitRepo,
    VaultConflict,
    VaultPushRejected,
)

_CONFLICT_MESSAGE = "The vault changed remotely in a way that conflicts with this save."
_REDACTED_PHRASE = "vault-test-redact-0000"


def test_is_clean_tracks_untracked_file(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    assert repo.is_clean()
    (setup.app / "scratch.md").write_text("scratch\n", encoding="utf-8")
    assert not repo.is_clean()


def test_head_matches_rev_parse(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    assert repo.head() == gitutil.head_of(setup.app)
    assert len(repo.head()) == 40


def test_commit_paths_commits_only_given_paths(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    rel_path = "Learning/Questions/q-demo.md"
    (setup.app / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (setup.app / rel_path).write_text("# Demo\n", encoding="utf-8")

    commit = repo.commit_paths([rel_path], "curio: add question q-demo")

    assert commit == gitutil.head_of(setup.app)
    gitutil.run_git("push", "origin", "HEAD:main", cwd=setup.app)
    assert commit == gitutil.head_of(setup.remote)
    identity = gitutil.run_git("log", "-1", "--format=%an %ae %cn %ce", cwd=setup.app)
    assert identity == f"{AUTHOR_NAME} {AUTHOR_EMAIL} {AUTHOR_NAME} {AUTHOR_EMAIL}"
    subject = gitutil.run_git("log", "-1", "--format=%s", cwd=setup.app)
    assert subject == "curio: add question q-demo"
    (setup.app / "unrelated.md").write_text("scratch\n", encoding="utf-8")
    changed = gitutil.run_git("show", "--name-only", "--format=", "HEAD", cwd=setup.app)
    assert changed.splitlines() == [rel_path]


def test_commit_paths_returns_none_when_unchanged(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    rel_path = "Learning/Questions/q-demo.md"
    (setup.app / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (setup.app / rel_path).write_text("# Demo\n", encoding="utf-8")
    first = repo.commit_paths([rel_path], "curio: add question q-demo")
    assert first is not None

    second = repo.commit_paths([rel_path], "curio: add question q-demo")

    assert second is None
    assert repo.head() == first


def test_pull_rebase_integrates_remote_change(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    gitutil.commit_and_push(setup.other, "Notes/Other.md", "## Other\n", message="device b")

    repo.pull_rebase()

    assert (setup.app / "Notes/Other.md").read_text(encoding="utf-8") == "## Other\n"
    assert repo.head() == gitutil.head_of(setup.remote)
    assert repo.is_clean()


def test_pull_rebase_conflict_raises_and_leaves_repo_clean(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    rel_path = "Learning/Questions/q-demo.md"
    (setup.app / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (setup.app / rel_path).write_text("A version\n", encoding="utf-8")
    repo.commit_paths([rel_path], "curio: gut answer q-demo")
    local_commit = gitutil.head_of(setup.app)
    gitutil.commit_and_push(setup.other, rel_path, "B version\n", message="device b edit")

    with pytest.raises(VaultConflict) as error:
        repo.pull_rebase()

    assert str(error.value) == _CONFLICT_MESSAGE
    assert error.value.detail
    assert repo.is_clean()
    assert repo.head() == local_commit
    assert not (setup.app / ".git" / "rebase-merge").exists()
    assert not (setup.app / ".git" / "rebase-apply").exists()


def test_push_failure_detail_is_redacted(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(
        setup.app,
        branch=gitutil.BRANCH,
        remote=f"/nonexistent/vault-{_REDACTED_PHRASE}.git",
        secrets=(_REDACTED_PHRASE,),
    )

    with pytest.raises(VaultPushRejected) as error:
        repo.push()

    assert str(error.value) == "The vault rejected the push."
    assert _REDACTED_PHRASE not in error.value.detail
    assert "***" in error.value.detail


def test_git_error_detail_is_redacted(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH, secrets=(_REDACTED_PHRASE,))

    with pytest.raises(GitError) as error:
        repo.run("fetch", f"/nonexistent/vault-{_REDACTED_PHRASE}.git")

    assert error.value.message == "git fetch failed"
    assert _REDACTED_PHRASE not in str(error.value)
    assert _REDACTED_PHRASE not in error.value.detail
    assert "vault-" in error.value.detail


def test_env_without_ssh_key(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    env = repo._env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["LC_ALL"] == "C"
    assert "GIT_SSH_COMMAND" not in env


def test_env_sets_git_ssh_command_when_key_exists(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    key = tmp_path / "keys" / "vault_key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("placeholder\n", encoding="utf-8")
    hosts = tmp_path / "keys" / "known_hosts"
    hosts.write_text("vault.example.invalid ssh-ed25519 AAAAnotreal0000\n", encoding="utf-8")
    repo = GitRepo(setup.app, branch=gitutil.BRANCH, ssh_key_file=key, known_hosts_file=hosts)

    ssh_command = repo._env()["GIT_SSH_COMMAND"]

    for fragment in (
        f"ssh -i {shlex.quote(str(key))}",
        "-o IdentitiesOnly=yes",
        "-o BatchMode=yes",
        "-o StrictHostKeyChecking=yes",
        f"-o UserKnownHostsFile={shlex.quote(str(hosts))}",
    ):
        assert fragment in ssh_command


def test_env_omits_ssh_command_when_key_missing(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH, ssh_key_file=tmp_path / "missing_key")
    assert "GIT_SSH_COMMAND" not in repo._env()


def test_reset_hard_restores_head_and_drops_untracked(tmp_path: Path) -> None:
    setup = gitutil.make_repos(tmp_path)
    repo = GitRepo(setup.app, branch=gitutil.BRANCH)
    base = repo.head()
    rel_path = "Learning/Questions/q-demo.md"
    (setup.app / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (setup.app / rel_path).write_text("will be reset\n", encoding="utf-8")
    repo.commit_paths([rel_path], "curio: add question q-demo")
    (setup.app / "untracked.md").write_text("untracked\n", encoding="utf-8")

    repo.reset_hard(base)

    assert repo.head() == base
    assert repo.is_clean()
    assert not (setup.app / rel_path).exists()
    assert not (setup.app / "untracked.md").exists()


def test_pull_failure_without_rebase_is_not_reported_as_conflict(tmp_path: Path) -> None:
    repo_path = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)  # noqa: S603, S607
    repo = GitRepo(repo_path, branch="main", remote="missing-remote")

    with pytest.raises(GitError) as error:
        repo.pull_rebase()

    assert not isinstance(error.value, VaultConflict)
    assert error.value.message == "Could not update the vault from its remote."
