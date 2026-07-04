"""Self-update service.

For a private git-installed deployment the update channel is the git
remote: the updater periodically fetches, compares HEAD to the upstream
branch, and either notifies (default) or — when ``auto_apply`` is on —
performs ``git pull --ff-only`` plus ``pip install -e .`` and asks systemd
to restart the service. Non-fast-forward situations are never forced; they
produce a notification for the human instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path

import structlog

from .core.config import UpdateConfig
from .core.events import EventBus, Topics

log = structlog.get_logger(__name__)


class UpdateService:
    def __init__(self, config: UpdateConfig, bus: EventBus, *, repo_dir: Path | None = None) -> None:
        self._config = config
        self._bus = bus
        self._repo = repo_dir or Path(__file__).resolve().parents[2]
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self.available: str | None = None

    async def _run(self, cmd: list[str]) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *cmd, cwd=self._repo,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        # Tracked so stop() can terminate an in-flight git/pip child instead of
        # cancelling only the await and leaving the process orphaned (which
        # could race a systemd restart against a half-finished pip install).
        self._proc = process
        try:
            output, _ = await process.communicate()
        finally:
            self._proc = None
        return process.returncode or 0, output.decode("utf-8", "replace").strip()

    @property
    def is_git_checkout(self) -> bool:
        return (self._repo / ".git").exists()

    async def start(self) -> None:
        if not self._config.enabled or not self.is_git_checkout:
            log.info("auto-update disabled or not a git checkout", repo=str(self._repo))
            return
        self._task = asyncio.create_task(self._loop(), name="updater")

    async def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            # Kill an in-flight git/pip child so it does not outlive shutdown.
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=5)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_once()
            except Exception:
                log.exception("update check failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self._config.check_interval_hours * 3600)

    async def check_once(self) -> str | None:
        """Fetch and compare. Returns the new upstream commit if one exists."""
        if not self.is_git_checkout:
            # Guard the direct callers (CLI, scheduler job): without a .git here
            # git would discover and pull an unrelated ancestor repo.
            log.info("not a git checkout; self-update unavailable", repo=str(self._repo))
            return None
        code, _ = await self._run(["git", "fetch", "--quiet"])
        if code != 0:
            return None
        code, local = await self._run(["git", "rev-parse", "HEAD"])
        code2, remote = await self._run(["git", "rev-parse", "@{upstream}"])
        if code != 0 or code2 != 0 or local == remote:
            self.available = None
            return None
        code, behind = await self._run(["git", "rev-list", "--count", "HEAD..@{upstream}"])
        if code != 0 or not behind.isdigit() or int(behind) == 0:
            # Ahead-only checkout (local commits, nothing new upstream) or a
            # failed count: local != remote but there is nothing to pull.
            self.available = None
            return None
        self.available = remote[:12]
        log.info("update available", commits_behind=behind, remote=remote[:12])
        if self._config.auto_apply:
            await self._apply()
        else:
            await self._bus.publish(Topics.NOTIFY, {
                "level": "info", "title": "Update available",
                "body": f"{behind} new commit(s) upstream ({remote[:12]}). "
                        "Run `poseidon update apply` or enable updates.auto_apply.",
            })
        return remote

    async def _apply(self) -> bool:
        if not self.is_git_checkout:
            log.warning("cannot apply update: not a git checkout", repo=str(self._repo))
            return False
        code, output = await self._run(["git", "pull", "--ff-only"])
        if code != 0:
            await self._bus.publish(Topics.NOTIFY, {
                "level": "warning", "title": "Update failed",
                "body": f"git pull --ff-only failed:\n{output[-500:]}",
            })
            return False
        # Use the interpreter actually running Poseidon (the venv), never a
        # bare "python" that PATH may resolve to a different environment.
        code, output = await self._run(
            [sys.executable, "-m", "pip", "install", "--quiet", "-e", "."]
        )
        if code != 0:
            await self._bus.publish(Topics.NOTIFY, {
                "level": "warning", "title": "Update install failed",
                "body": output[-500:],
            })
            return False
        await self._bus.publish(Topics.NOTIFY, {
            "level": "info", "title": "Update applied",
            "body": "New version installed. Restart the service to activate "
                    "(systemctl --user restart poseidon).",
        })
        return True
