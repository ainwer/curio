"""Helpers building temporary remote/clone git topologies for vault tests."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from curio_app import db

BRANCH = "main"


@dataclass(frozen=True)
class RepoSet:
    """A bare remote plus two clones (app checkout and another device)."""

    remote: Path
    app: Path
    other: Path


def run_git(*args: str, cwd: Path) -> str:
    """Run git in *cwd* with a test identity; return stripped stdout."""
    subprocess_run = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@localhost",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if subprocess_run.returncode != 0:
        raise AssertionError(f"git {args} failed in {cwd}: {subprocess_run.stderr}")
    return subprocess_run.stdout.strip()


def head_of(cwd: Path) -> str:
    """Return the current commit of *cwd*."""
    return run_git("rev-parse", "HEAD", cwd=cwd)


def commit_and_push(cwd: Path, rel_path: str, content: str, message: str = "other device") -> None:
    """Write, commit and push one file from a clone."""
    file = cwd / rel_path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    run_git("add", "--", rel_path, cwd=cwd)
    run_git("commit", "-m", message, "--", rel_path, cwd=cwd)
    run_git("push", "origin", f"HEAD:{BRANCH}", cwd=cwd)


def pull_rebase(cwd: Path) -> None:
    """Sync a clone with origin."""
    run_git("pull", "--rebase", "--no-autostash", "origin", BRANCH, cwd=cwd)


def make_repos(base: Path) -> RepoSet:
    """Create a bare remote at base/remote.git and clone it into base/app and base/other."""
    seed = base / "seed"
    seed.mkdir(parents=True)
    run_git("init", "-b", BRANCH, str(seed), cwd=base)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    run_git("add", ".", cwd=seed)
    run_git("commit", "-m", "seed", cwd=seed)
    remote = base / "remote.git"
    run_git("clone", "--bare", str(seed), str(remote), cwd=base)
    app = base / "app"
    other = base / "other"
    run_git("clone", str(remote), str(app), cwd=base)
    run_git("clone", str(remote), str(other), cwd=base)
    return RepoSet(remote=remote, app=app, other=other)


def make_db(db_path: Path) -> Path:
    """Create and migrate an app database, then close it."""
    conn = db.connect(db_path)
    try:
        db.migrate(conn)
    finally:
        conn.close()
    return db_path
