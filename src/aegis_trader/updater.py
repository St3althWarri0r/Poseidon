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
from pathlib import Path

import structlog

from .core.config import UpdateConfig
from .core.events import EventBus, Topics

log = structlog.get_logger(__name__)


async def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    return process.returncode or 0, output.decode("utf-8", "replace").strip()


class UpdateService:
    def __init__(self, config: UpdateConfig, bus: EventBus, *, repo_dir: Path | None = None) -> None:
        self._config = config
        self._bus = bus
        self._repo = repo_dir or Path(__file__).resolve().parents[2]
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.available: str | None = None

    async def start(self) -> None:
        if not self._config.enabled or not (self._repo / ".git").exists():
            log.info("auto-update disabled or not a git checkout", repo=str(self._repo))
            return
        self._task = asyncio.create_task(self._loop(), name="updater")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.check_once()
            except Exception:
                log.exception("update check failed")
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=self._config.check_interval_hours * 3600)
            except TimeoutError:
                pass

    async def check_once(self) -> str | None:
        """Fetch and compare. Returns the new upstream commit if one exists."""
        code, _ = await _run(["git", "fetch", "--quiet"], self._repo)
        if code != 0:
            return None
        code, local = await _run(["git", "rev-parse", "HEAD"], self._repo)
        code2, remote = await _run(["git", "rev-parse", "@{upstream}"], self._repo)
        if code != 0 or code2 != 0 or local == remote:
            self.available = None
            return None
        code, behind = await _run(["git", "rev-list", "--count", "HEAD..@{upstream}"], self._repo)
        self.available = remote[:12]
        log.info("update available", commits_behind=behind, remote=remote[:12])
        if self._config.auto_apply:
            await self._apply()
        else:
            await self._bus.publish(Topics.NOTIFY, {
                "level": "info", "title": "Update available",
                "body": f"{behind} new commit(s) upstream ({remote[:12]}). "
                        "Run `aegis update apply` or enable updates.auto_apply.",
            })
        return remote

    async def _apply(self) -> bool:
        code, output = await _run(["git", "pull", "--ff-only"], self._repo)
        if code != 0:
            await self._bus.publish(Topics.NOTIFY, {
                "level": "warning", "title": "Update failed",
                "body": f"git pull --ff-only failed:\n{output[-500:]}",
            })
            return False
        code, output = await _run(
            ["python", "-m", "pip", "install", "--quiet", "-e", "."], self._repo
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
                    "(systemctl --user restart aegis-trader).",
        })
        return True
