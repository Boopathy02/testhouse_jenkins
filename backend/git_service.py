from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict
from urllib.parse import quote, urlparse, urlunparse


def _failure(message: str, branch: str | None = None) -> Dict[str, str]:
    return {
        "status": "failure",
        "branch_name": branch or "",
        "commit_hash": "",
        "error_message": message,
    }


def _sanitize_message(message: str, token: str | None) -> str:
    if not message:
        return message
    if token:
        return message.replace(token, "***")
    return message


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed{f': {detail}' if detail else ''}")
    return result


def _build_auth_url(repo_url: str, username: str, token: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.scheme.lower() != "https":
        raise ValueError("Only HTTPS Git URLs are supported.")
    if not parsed.hostname:
        raise ValueError("Invalid repo_url.")
    netloc = parsed.hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    userinfo = f"{quote(username, safe='')}:{quote(token, safe='')}"
    return urlunparse(
        (
            "https",
            f"{userinfo}@{netloc}",
            parsed.path or "",
            parsed.params or "",
            parsed.query or "",
            parsed.fragment or "",
        )
    )


def _copy_generated_runs(source: Path, repo_root: Path) -> list[str]:
    copied: list[str] = []
    source = source.resolve()
    repo_root = repo_root.resolve()
    dest_root = repo_root / "generated_runs"
    dest_root.mkdir(parents=True, exist_ok=True)

    for src_path in source.rglob("*"):
        if not src_path.is_file():
            continue
        rel = src_path.relative_to(source)
        dest_path = dest_root / rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        copied.append(dest_path.relative_to(repo_root).as_posix())

    return copied


def push_generated_project(config: dict) -> dict:
    try:
        repo_url = (config.get("repo_url") or "").strip()
        base_branch = (config.get("base_branch") or "").strip()
        target_branch = (config.get("target_branch") or "").strip()
        git_username = (config.get("git_username") or "").strip()
        git_token_env = (config.get("git_token_env") or "").strip()
        commit_message = (config.get("commit_message") or "").strip()
        author_name = (config.get("author_name") or "").strip()
        author_email = (config.get("author_email") or "").strip()
        generated_runs_path = (config.get("generated_runs_path") or "").strip()

        if not repo_url:
            return _failure("repo_url is required.", target_branch)
        if not base_branch:
            return _failure("base_branch is required.", target_branch)
        if not target_branch:
            return _failure("target_branch is required.", target_branch)
        if target_branch.lower() in {"main", "master"}:
            return _failure("target_branch cannot be main/master.", target_branch)
        if not git_username:
            return _failure("git_username is required.", target_branch)
        if not git_token_env:
            return _failure("git_token_env is required.", target_branch)
        if not commit_message:
            return _failure("commit_message is required.", target_branch)
        if not author_name or not author_email:
            return _failure("author_name and author_email are required.", target_branch)
        if not generated_runs_path:
            return _failure("generated_runs_path is required.", target_branch)

        token = os.getenv(git_token_env)
        if not token:
            return _failure(f"Missing token env var: {git_token_env}", target_branch)

        runs_root = Path(generated_runs_path).resolve()
        if not runs_root.exists() or not runs_root.is_dir():
            return _failure("generated_runs_path not found.", target_branch)

        auth_url = _build_auth_url(repo_url, git_username, token)

        with tempfile.TemporaryDirectory(prefix="git_push_") as tmpdir:
            repo_root = Path(tmpdir)

            _run_git(["clone", auth_url, "."], repo_root)
            base_head = _run_git(
                ["ls-remote", "--heads", "origin", base_branch],
                repo_root,
            ).stdout.strip()
            if not base_head:
                return _failure(
                    f"base_branch not found on remote: {base_branch}",
                    target_branch,
                )
            _run_git(["fetch", "origin", base_branch], repo_root)
            _run_git(["checkout", "-B", base_branch, f"origin/{base_branch}"], repo_root)

            remote_heads = _run_git(
                ["ls-remote", "--heads", "origin", target_branch],
                repo_root,
            ).stdout.strip()
            if remote_heads:
                _run_git(["checkout", "-B", target_branch, f"origin/{target_branch}"], repo_root)
            else:
                _run_git(["checkout", "-B", target_branch], repo_root)

            copied = _copy_generated_runs(runs_root, repo_root)
            if not copied:
                return _failure("No generated files found to push.", target_branch)

            _run_git(["config", "user.name", author_name], repo_root)
            _run_git(["config", "user.email", author_email], repo_root)
            _run_git(["add", "--", "generated_runs"], repo_root)

            status = _run_git(["status", "--porcelain"], repo_root).stdout.strip()
            if not status:
                return _failure("No changes detected after staging.", target_branch)

            _run_git(["commit", "-m", commit_message, "--author", f"{author_name} <{author_email}>"], repo_root)
            commit_hash = _run_git(["rev-parse", "HEAD"], repo_root).stdout.strip()
            _run_git(["push", "origin", target_branch], repo_root)

        return {
            "status": "success",
            "branch_name": target_branch,
            "commit_hash": commit_hash,
            "error_message": "",
        }
    except Exception as exc:
        token = None
        git_token_env = config.get("git_token_env") if isinstance(config, dict) else None
        if git_token_env:
            token = os.getenv(git_token_env)
        return _failure(_sanitize_message(str(exc), token), config.get("target_branch") or "")
