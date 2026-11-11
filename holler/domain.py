"""Plain dataclasses for sessions/turns/tool_calls. Used by `Store`."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Session:
    id: int
    sdk_session_id: Optional[str]
    title: str
    transcript: str
    screenshot_path: Optional[str]
    model: str
    created_at: float
    last_activity_at: float
    status: str
    pinned: bool
    archived: bool
    notes: str
    # True iff the current title was set by AutoTitler (rolling re-title may
    # refresh it). False iff the user renamed via the right-click action.
    auto_titled: bool = True
    # Name of the profile MD that was layered into the session's system prompt
    # at creation time. None = no profile (default holler behavior).
    profile: Optional[str] = None
    # Epoch when the user marked the session as reviewed/done. None = still
    # active (un-reviewed). Drives the default-view filter.
    reviewed_at: Optional[float] = None
    # Count of auto-title attempts so far (success or failure both count). The
    # AutoTitler caps retries by this counter so a permanently failing API
    # doesn't pound the network forever.
    title_attempts: int = 0
    # Epoch of the most recent auto-title attempt. Used by the launch-time
    # backfill to enforce a minimum cooldown between retries.
    title_last_attempt_at: float = 0.0
    # Per-session worktree (Stage 1 concurrency model). Empty path/branch with
    # state='none' = session uses the shared main checkout (legacy behavior).
    # When per_session_worktrees is on, WorktreeManager populates these fields.
    worktree_path: str = ""
    worktree_branch: str = ""
    # 'none' | 'active' | 'merged' | 'orphaned' — see _v8_worktree migration.
    worktree_state: str = "none"
    # Repo this session works in (v15). '' = the default repo (cfg.repo_cwd),
    # which keeps following Settings changes exactly as before projects
    # existed; a path = a configured project repo (e.g. myproject).
    repo_cwd: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Session":
        # auto_titled column was added in migration v4 — fall back to True for
        # rows from older databases that haven't been re-read after migration.
        try:
            auto_titled = bool(row["auto_titled"])
        except (IndexError, KeyError):
            auto_titled = True
        # profile column added in v5; fall back to None for older rows.
        try:
            profile = row["profile"]
            profile = profile if profile else None
        except (IndexError, KeyError):
            profile = None
        # reviewed_at column added in v6; fall back to None for older rows.
        try:
            reviewed_at = row["reviewed_at"]
            reviewed_at = float(reviewed_at) if reviewed_at is not None else None
        except (IndexError, KeyError):
            reviewed_at = None
        # title_attempts/title_last_attempt_at added in v7; fall back to 0
        # for rows from older DBs.
        try:
            title_attempts = int(row["title_attempts"] or 0)
        except (IndexError, KeyError, TypeError):
            title_attempts = 0
        try:
            title_last_attempt_at = float(row["title_last_attempt_at"] or 0)
        except (IndexError, KeyError, TypeError):
            title_last_attempt_at = 0.0
        # worktree_path/branch/state added in v8; fall back to empty strings
        # and 'none' for older rows.
        try:
            worktree_path = row["worktree_path"] or ""
        except (IndexError, KeyError):
            worktree_path = ""
        try:
            worktree_branch = row["worktree_branch"] or ""
        except (IndexError, KeyError):
            worktree_branch = ""
        try:
            worktree_state = row["worktree_state"] or "none"
        except (IndexError, KeyError):
            worktree_state = "none"
        try:
            repo_cwd = row["repo_cwd"] or ""
        except (IndexError, KeyError):
            repo_cwd = ""
        return Session(
            id=int(row["id"]),
            sdk_session_id=row["sdk_session_id"],
            title=row["title"] or "",
            transcript=row["transcript"] or "",
            screenshot_path=row["screenshot_path"],
            model=row["model"] or "",
            created_at=float(row["created_at"]),
            last_activity_at=float(row["last_activity_at"]),
            status=row["status"] or "idle",
            pinned=bool(row["pinned"]),
            archived=bool(row["archived"]),
            notes=row["notes"] or "",
            auto_titled=auto_titled,
            profile=profile,
            reviewed_at=reviewed_at,
            title_attempts=title_attempts,
            title_last_attempt_at=title_last_attempt_at,
            worktree_path=worktree_path,
            worktree_branch=worktree_branch,
            worktree_state=worktree_state,
            repo_cwd=repo_cwd,
        )


@dataclass(frozen=True)
class Turn:
    id: int
    session_id: int
    seq: int
    transcript: str
    screenshot_path: Optional[str]
    final_text: str
    duration_ms: int
    started_at: float
    ended_at: float
    is_error: bool
    job_id: Optional[int]
    # JSON-encoded ordered content blocks (text segments + tool_call indices)
    # added in v14. '' = not recorded → committed renderer uses the legacy
    # "tools, then final_text" layout.
    blocks_json: str = ""
    # Model id the turn ran on ('' for turns recorded before it was written).
    model: str = ""

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Turn":
        # blocks_json column added in v14; fall back to '' for older rows.
        try:
            blocks_json = row["blocks_json"] or ""
        except (IndexError, KeyError):
            blocks_json = ""
        try:
            model = row["model"] or ""
        except (IndexError, KeyError):
            model = ""
        return Turn(
            id=int(row["id"]),
            session_id=int(row["session_id"]),
            seq=int(row["seq"]),
            transcript=row["transcript"] or "",
            screenshot_path=row["screenshot_path"],
            final_text=row["final_text"] or "",
            duration_ms=int(row["duration_ms"] or 0),
            started_at=float(row["started_at"]),
            ended_at=float(row["ended_at"] or 0),
            is_error=bool(row["is_error"]),
            job_id=int(row["job_id"]) if row["job_id"] is not None else None,
            blocks_json=blocks_json,
            model=model,
        )


@dataclass(frozen=True)
class ToolCallRecord:
    id: int
    turn_id: int
    name: str
    input_json: str
    result_json: str
    is_error: bool
    started_at: float
    ended_at: float

    @staticmethod
    def from_row(row: sqlite3.Row) -> "ToolCallRecord":
        return ToolCallRecord(
            id=int(row["id"]),
            turn_id=int(row["turn_id"]),
            name=row["name"] or "",
            input_json=row["input_json"] or "",
            result_json=row["result_json"] or "",
            is_error=bool(row["is_error"]),
            started_at=float(row["started_at"]),
            ended_at=float(row["ended_at"] or 0),
        )
