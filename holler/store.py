"""SQLite persistence layer for sessions, turns, and tool calls.

WAL mode + connection-per-thread: every method opens (or fetches) a connection
keyed on the calling thread via `threading.local()`. This pattern keeps GUI-
thread reads non-blocking against the SDK-thread streaming writes.

Schema migrations are versioned via `PRAGMA user_version`. Add a new function
to `_MIGRATIONS` for each forward-compatible change; never edit a past entry.

FTS5 is required for search. We probe at startup; if disabled in this Python's
sqlite3 build, `search()` falls back to LIKE.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from holler.domain import Session, ToolCallRecord, Turn

log = logging.getLogger(__name__)


def _v1_initial(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sdk_session_id TEXT UNIQUE,
            title TEXT NOT NULL DEFAULT '',
            transcript TEXT NOT NULL DEFAULT '',
            screenshot_path TEXT,
            model TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            last_activity_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'idle',
            pinned INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_last_activity
            ON sessions(last_activity_at DESC);
        CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
        CREATE INDEX IF NOT EXISTS idx_sessions_pinned ON sessions(pinned);

        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            transcript TEXT NOT NULL DEFAULT '',
            screenshot_path TEXT,
            final_text TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL,
            ended_at REAL NOT NULL DEFAULT 0,
            is_error INTEGER NOT NULL DEFAULT 0,
            job_id INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, seq);

        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            input_json TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL DEFAULT '',
            is_error INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL,
            ended_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_tool_calls_turn ON tool_calls(turn_id);
        """
    )


def _v2_indexes(c: sqlite3.Connection) -> None:
    """Add indexes that v1 missed; covers list_sessions and crash_recovery hot paths."""
    c.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_sessions_archived_activity
            ON sessions(archived, last_activity_at DESC);
        CREATE INDEX IF NOT EXISTS idx_sessions_pinned_archived
            ON sessions(pinned, archived, last_activity_at DESC);
        CREATE INDEX IF NOT EXISTS idx_turns_open_started
            ON turns(started_at) WHERE ended_at = 0;
        """
    )


def _v3_turn_usage(c: sqlite3.Connection) -> None:
    """Persist per-turn token usage so sessions can show lifetime totals."""
    c.executescript(
        """
        ALTER TABLE turns ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE turns ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE turns ADD COLUMN cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE turns ADD COLUMN cache_read_input_tokens INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE turns ADD COLUMN total_cost_usd REAL NOT NULL DEFAULT 0;
        """
    )


def _v4_auto_titled(c: sqlite3.Connection) -> None:
    """Track which session titles came from the AutoTitler vs a user rename.

    Existing rows default to 1 (auto). The right-click Rename action sets it
    to 0 so rolling re-title never overwrites a user's chosen title.
    """
    c.executescript(
        "ALTER TABLE sessions ADD COLUMN auto_titled INTEGER NOT NULL DEFAULT 1;"
    )


def _v5_profile(c: sqlite3.Connection) -> None:
    """Per-session "profile" — name of the operating-mode MD that was layered
    into the system prompt at creation time. NULL = no profile (default).
    Resolved against built-in + user profile directories at run time."""
    c.executescript(
        "ALTER TABLE sessions ADD COLUMN profile TEXT;"
    )


def _v6_reviewed(c: sqlite3.Connection) -> None:
    """Per-session "reviewed_at" — epoch when the user marked the session as
    Done (reviewed). NULL = un-reviewed (active). Used to filter the default
    rail view so finished sessions stop crowding TODAY."""
    c.executescript(
        "ALTER TABLE sessions ADD COLUMN reviewed_at REAL;"
        "CREATE INDEX IF NOT EXISTS idx_sessions_reviewed_activity "
        "ON sessions(reviewed_at, last_activity_at DESC);"
    )


def _v7_title_meta(c: sqlite3.Connection) -> None:
    """Track auto-title attempts so we can retry-with-backoff and stop pounding
    the API after N failures. attempts=0 means "never tried" — the runner kicks
    a titler at session creation and increments here. last_attempt_at is the
    epoch of the most recent try; the cooldown uses it for backoff."""
    c.executescript(
        "ALTER TABLE sessions ADD COLUMN title_attempts INTEGER NOT NULL DEFAULT 0;"
        "ALTER TABLE sessions ADD COLUMN title_last_attempt_at REAL NOT NULL DEFAULT 0;"
    )


def _v8_worktree(c: sqlite3.Connection) -> None:
    """Per-session git worktree tracking — enables true concurrent project edits.

    Each Holler session that touches a shared git checkout can claim its own
    git worktree under <main-repo>/.claude/worktrees/holler-<id>-<slug>/ with
    a dedicated holler/<id>-<slug> branch. The three columns let
    WorktreeManager survive a Holler restart: on launch we reconcile rows
    where worktree_state='active' against `git worktree list --porcelain` and
    flag any mismatches as 'orphaned' for operator cleanup.

    worktree_path:   filesystem path to the worktree dir ('' = no worktree)
    worktree_branch: branch checked out in that worktree ('' = no worktree)
    worktree_state:  'none' | 'active' | 'merged' | 'orphaned'
                     - 'none': session has never claimed a worktree
                     - 'active': worktree exists on disk and is in use
                     - 'merged': branch landed via git cleanly; worktree torn down
                     - 'orphaned': DB says active but worktree is missing on disk
    """
    c.executescript(
        "ALTER TABLE sessions ADD COLUMN worktree_path TEXT NOT NULL DEFAULT '';"
        "ALTER TABLE sessions ADD COLUMN worktree_branch TEXT NOT NULL DEFAULT '';"
        "ALTER TABLE sessions ADD COLUMN worktree_state TEXT NOT NULL DEFAULT 'none';"
        "CREATE INDEX IF NOT EXISTS idx_sessions_worktree_state "
        "ON sessions(worktree_state) WHERE worktree_state != 'none';"
    )


def _v9_superintendent_evidence(c: sqlite3.Connection) -> None:
    """Superintendent evidence ledger — the single-writer completeness spine.

    Every operator PTT into the Superintendent inbox lands one row here. The
    dispatcher is the sole mutator of `status` after insert, so "did anything
    fall through the cracks" is answered by a COUNT, not by hope. Lifecycle:
        queued -> dispatching -> working -> deployed | failed

    `dispatch_session_id` correlates the row to the worker session that appears
    in the left rail; `worktree_path` is stamped for post-mortem cleanup.
    """
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS superintendent_evidence (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at          REAL NOT NULL,
            updated_at          REAL NOT NULL,
            status              TEXT NOT NULL DEFAULT 'queued',
            transcript          TEXT NOT NULL DEFAULT '',
            screenshot_path     TEXT,
            inbox_session_id    INTEGER,
            inbox_turn_id       INTEGER,
            dispatch_session_id INTEGER,
            worktree_path       TEXT NOT NULL DEFAULT '',
            error               TEXT NOT NULL DEFAULT '',
            dispatched_at       REAL NOT NULL DEFAULT 0,
            finished_at         REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_superintendent_evidence_status
            ON superintendent_evidence(status, id);
        """
    )


def _v10_evidence_screenshot_paths(c: sqlite3.Connection) -> None:
    """Multi-screenshot chaining for evidence rows.

    One issue can now carry several screenshots (voice-chained by the operator).
    `screenshot_paths` holds the full JSON array; `screenshot_path` stays as the
    primary/first shot for back-compat with existing readers.
    """
    cols = {r["name"] for r in c.execute("PRAGMA table_info(superintendent_evidence)")}
    if "screenshot_paths" not in cols:
        c.execute("ALTER TABLE superintendent_evidence ADD COLUMN screenshot_paths TEXT")


def _v11_superintendent_evidence_repair(c: sqlite3.Connection) -> None:
    """Repair: guarantee the evidence table exists on DBs that reached v10
    before the superintendent migrations were introduced.

    The Mac fork's DB was already stamped user_version=10 by earlier (non-
    superintendent) migrations, so `_v9`/`_v10` never ran and the evidence
    table was silently absent — the dispatcher raised `no such table` on every
    tick. This idempotent migration creates the full final-state table so both
    fresh installs (where v9/v10 already built it) and pre-existing DBs converge
    on the same schema. It is a no-op when the table is already present.
    """
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS superintendent_evidence (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at          REAL NOT NULL,
            updated_at          REAL NOT NULL,
            status              TEXT NOT NULL DEFAULT 'queued',
            transcript          TEXT NOT NULL DEFAULT '',
            screenshot_path     TEXT,
            inbox_session_id    INTEGER,
            inbox_turn_id       INTEGER,
            dispatch_session_id INTEGER,
            worktree_path       TEXT NOT NULL DEFAULT '',
            error               TEXT NOT NULL DEFAULT '',
            dispatched_at       REAL NOT NULL DEFAULT 0,
            finished_at         REAL NOT NULL DEFAULT 0,
            screenshot_paths    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_superintendent_evidence_status
            ON superintendent_evidence(status, id);
        """
    )
    cols = {r["name"] for r in c.execute("PRAGMA table_info(superintendent_evidence)")}
    if "screenshot_paths" not in cols:
        c.execute("ALTER TABLE superintendent_evidence ADD COLUMN screenshot_paths TEXT")


def _v12_evidence_mode(c: sqlite3.Connection) -> None:
    """Evidence lane selector: 'deploy' (default) vs 'investigation' (IR).

    A 'deploy' row spawns the autonomous 6-step remediation worker that lands to
    prod. An 'investigation' row spawns the IR/forensics worker that analyses the
    chained evidence, may prototype a fix in its own worktree, and reports back a
    terse expert summary — it never deploys. Pre-existing rows are 'deploy'.
    """
    cols = {r["name"] for r in c.execute("PRAGMA table_info(superintendent_evidence)")}
    if "mode" not in cols:
        c.execute(
            "ALTER TABLE superintendent_evidence "
            "ADD COLUMN mode TEXT NOT NULL DEFAULT 'deploy'"
        )


def _v13_fts_resync(c: sqlite3.Connection) -> None:
    """Rebuild the contentless FTS index from the canonical tables.

    Earlier set_title/update_turn passed '' instead of the previously-indexed
    final_text to FTS5's 'delete' command, so old tokens (stale titles, earlier
    final_text aggregates) stayed searchable and per-row stats drifted. A
    contentless table can't use 'rebuild', so: 'delete-all' then re-insert
    every session's (title, transcript, aggregated final_text), all inside one
    transaction. No schema change. A no-op when the FTS table doesn't exist
    yet (fresh DB — _ensure_fts creates it after migrations) or FTS5 isn't
    compiled in.
    """
    exists = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions_fts'"
    ).fetchone()
    if not exists or not _detect_fts5(c):
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("INSERT INTO sessions_fts(sessions_fts) VALUES('delete-all')")
        c.execute(
            "INSERT INTO sessions_fts(rowid, title, transcript, final_text) "
            "SELECT s.id, s.title, s.transcript, "
            f"  ({_FTS_FINAL_TEXT_SQL.replace('?', 's.id')}) "
            "FROM sessions s"
        )
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise


def _v14_turn_blocks(c: sqlite3.Connection) -> None:
    """Ordered content-block sequence per turn, so committed turns render
    assistant text interleaved with tool calls instead of "all tools, then
    final_text" (final_text is only the SDK result — text written before or
    between tool calls was lost once the in-memory Job went away).

    JSON list of {"t": "text", "text": ...} / {"t": "tool", "i": <index into
    the turn's tool_calls in id order>}. '' = not recorded (pre-v14 rows,
    merged turns) → renderer falls back to the legacy layout. Constant
    default → O(1) ALTER (no table rewrite on large DBs); column check keeps
    it idempotent.
    """
    cols = {r["name"] for r in c.execute("PRAGMA table_info(turns)")}
    if "blocks_json" not in cols:
        c.execute("ALTER TABLE turns ADD COLUMN blocks_json TEXT NOT NULL DEFAULT ''")


def _v15_session_repo(c: sqlite3.Connection) -> None:
    """Multi-repo sessions: which repo a session (and an evidence item) targets.

    sessions.repo_cwd: '' = the default repo (cfg.repo_cwd, legacy behaviour);
    otherwise the project repo the session's worktree is cut from.
    superintendent_evidence.project: '' = the superintendent profile's default
    project; otherwise a configured project name (per-request override).

    Column checks keep it idempotent: some existing DBs already carry a
    `sessions.repo_cwd` (same shape) from an earlier upstream migration.
    """
    cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    if "repo_cwd" not in cols:
        c.execute("ALTER TABLE sessions ADD COLUMN repo_cwd TEXT NOT NULL DEFAULT ''")
    ev = {r["name"] for r in c.execute("PRAGMA table_info(superintendent_evidence)")}
    if ev and "project" not in ev:
        c.execute(
            "ALTER TABLE superintendent_evidence ADD COLUMN project TEXT NOT NULL DEFAULT ''"
        )


# Aggregated per-session final_text exactly as it is fed to the FTS index.
# Every FTS 'delete' must pass the same values that were inserted, so all
# writers compute it through this one expression (ordered by seq for
# determinism).
_FTS_FINAL_TEXT_SQL = (
    "SELECT COALESCE(GROUP_CONCAT(final_text, ' '), '') FROM "
    "(SELECT final_text FROM turns WHERE session_id = ? ORDER BY seq)"
)


_MIGRATIONS: List[Callable[[sqlite3.Connection], None]] = [
    _v1_initial,
    _v2_indexes,
    _v3_turn_usage,
    _v4_auto_titled,
    _v5_profile,
    _v6_reviewed,
    _v7_title_meta,
    _v8_worktree,
    _v9_superintendent_evidence,
    _v10_evidence_screenshot_paths,
    _v11_superintendent_evidence_repair,
    _v12_evidence_mode,
    _v13_fts_resync,
    _v14_turn_blocks,
    _v15_session_repo,
]


def _detect_fts5(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return False


def _ensure_fts(conn: sqlite3.Connection) -> bool:
    """Create FTS5 virtual table + sync triggers if available. Returns success."""
    if not _detect_fts5(conn):
        return False
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
            title, transcript, final_text,
            content='', tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    return True


class Store:
    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        # sqlite3.Connection doesn't support weakrefs, so we keep strong refs.
        # Connections are closed in Store.close().
        self._all_conns: set[sqlite3.Connection] = set()
        self._all_conns_lock = threading.Lock()
        self._fts_enabled = False
        self._migrate()
        self._fts_enabled = _ensure_fts(self._conn())
        if not self._fts_enabled:
            log.info("FTS5 not available; falling back to LIKE-based search")
        self._crash_recovery()

    # --- connection management ---

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=True)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=2000")
            self._tls.conn = c
            with self._all_conns_lock:
                self._all_conns.add(c)
        return c

    def close_thread_connection(self) -> None:
        """Close and forget the calling thread's connection (no-op if none).

        Short-lived worker threads should call this before exiting; otherwise
        their connection lingers in `_all_conns` until `close()`. A later call
        from the same thread transparently opens a fresh connection.
        """
        c = getattr(self._tls, "conn", None)
        if c is None:
            return
        self._tls.conn = None
        self._tls.tx_depth = 0
        with self._all_conns_lock:
            self._all_conns.discard(c)
        try:
            if c.in_transaction:
                c.execute("ROLLBACK")
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._all_conns_lock:
            for c in list(self._all_conns):
                try:
                    c.close()
                except Exception:
                    pass
            self._all_conns.clear()
        # Only the calling thread's TLS slot can be reset; other threads'
        # stale (closed) handles would raise on use, which is correct after
        # close().
        self._tls.conn = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Explicit write transaction: BEGIN IMMEDIATE / COMMIT / ROLLBACK.

        Connections run with isolation_level=None (autocommit), so the old
        `with c:` blocks were NOT transactions — each statement committed on
        its own. This takes the write lock up front (no SHARED->RESERVED
        upgrade deadlock under WAL) and makes multi-statement writes atomic.

        Re-entrant per thread: a nested `_tx()` (e.g. a write method called
        from inside another's transaction) joins the outer transaction rather
        than issuing a second BEGIN; only the outermost commits/rolls back.
        """
        c = self._conn()
        depth = getattr(self._tls, "tx_depth", 0)
        if depth > 0 or c.in_transaction:
            self._tls.tx_depth = depth + 1
            try:
                yield c
            finally:
                self._tls.tx_depth = depth
            return
        c.execute("BEGIN IMMEDIATE")
        self._tls.tx_depth = 1
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                try:
                    c.execute("ROLLBACK")
                except sqlite3.Error:
                    log.exception("rollback failed")
            raise
        finally:
            self._tls.tx_depth = 0

    # --- FTS bookkeeping ---
    #
    # sessions_fts is contentless (content=''): rows can only be removed with
    # the special 'delete' command carrying the EXACT values that were
    # indexed. Invariant maintained by every writer: the indexed values for
    # session `sid` are (sessions.title, sessions.transcript,
    # _FTS_FINAL_TEXT_SQL(sid)). So: read via `_fts_values` BEFORE mutating,
    # `_fts_delete` those, mutate, then `_fts_insert` the fresh values — all in
    # one transaction.

    @staticmethod
    def _fts_values(c: sqlite3.Connection, session_id: int) -> Optional[tuple]:
        row = c.execute(
            "SELECT title, transcript FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        agg = c.execute(_FTS_FINAL_TEXT_SQL, (session_id,)).fetchone()[0]
        return (row["title"], row["transcript"], agg or "")

    @staticmethod
    def _fts_delete(c: sqlite3.Connection, session_id: int, values: tuple) -> None:
        c.execute(
            "INSERT INTO sessions_fts(sessions_fts, rowid, title, transcript, final_text) "
            "VALUES('delete', ?, ?, ?, ?)",
            (session_id, *values),
        )

    @staticmethod
    def _fts_insert(c: sqlite3.Connection, session_id: int, values: tuple) -> None:
        c.execute(
            "INSERT INTO sessions_fts(rowid, title, transcript, final_text) "
            "VALUES (?, ?, ?, ?)",
            (session_id, *values),
        )

    # --- migrations ---

    def _migrate(self) -> None:
        c = self._conn()
        version_row = c.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0]) if version_row else 0
        for i, fn in enumerate(_MIGRATIONS, start=1):
            if version < i:
                log.info("running migration v%d", i)
                fn(c)
                c.execute(f"PRAGMA user_version = {i}")

    def _crash_recovery(self) -> None:
        """Sweep turns that started but never ended (process killed mid-stream)."""
        now = time.time()
        with self._tx() as c:
            c.execute(
                "UPDATE turns SET is_error = 1, ended_at = ? "
                "WHERE ended_at = 0 AND started_at > 0 AND started_at < ?",
                (now, now - 1.0),  # only sweep things older than 1s — current jobs are not stale
            )
            c.execute(
                "UPDATE sessions SET status = 'idle' WHERE status = 'running'"
            )

    # --- writes ---

    def create_session(
        self,
        *,
        model: str,
        transcript: str,
        screenshot_path: Optional[str],
        status: str = "running",
        title: Optional[str] = None,
        profile: Optional[str] = None,
        repo_cwd: Optional[str] = None,
    ) -> int:
        """`repo_cwd`: None/'' = the default repo (cfg.repo_cwd); a path pins
        the session to that project repo for its whole life."""
        now = time.time()
        # Default title: first 60 chars of transcript.
        if title is None:
            title = (transcript or "").strip().split("\n", 1)[0][:60] or "Untitled session"
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO sessions(sdk_session_id, title, transcript, screenshot_path, "
                "model, created_at, last_activity_at, status, pinned, archived, notes, profile, "
                "repo_cwd) "
                "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 0, 0, '', ?, ?)",
                (title, transcript, screenshot_path, model, now, now, status, profile,
                 str(repo_cwd or "")),
            )
            session_id = int(cur.lastrowid or 0)
            if self._fts_enabled:
                c.execute(
                    "INSERT INTO sessions_fts(rowid, title, transcript, final_text) "
                    "VALUES (?, ?, ?, '')",
                    (session_id, title, transcript),
                )
        return session_id

    def attach_sdk_session_id(self, session_id: int, sdk_session_id: str) -> None:
        if not sdk_session_id:
            return
        with self._tx() as c:
            try:
                c.execute(
                    "UPDATE sessions SET sdk_session_id = ?, last_activity_at = ? "
                    "WHERE id = ? AND (sdk_session_id IS NULL OR sdk_session_id = ?)",
                    (sdk_session_id, time.time(), session_id, sdk_session_id),
                )
            except sqlite3.IntegrityError:
                # Conflict — another row already has this sdk_session_id.
                log.warning("sdk_session_id %s already attached to a different session", sdk_session_id)

    def clear_sdk_session_id(self, session_id: int) -> None:
        """Drop a session's sdk_session_id (NULL it). Used when Claude Code's
        JSONL store has lost the conversation and resume attempts will keep
        failing — clearing lets the next turn start a fresh SDK session in the
        same Holler row."""
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET sdk_session_id = NULL, last_activity_at = ? "
                "WHERE id = ?",
                (time.time(), session_id),
            )

    def add_turn(
        self,
        *,
        session_id: int,
        transcript: str,
        screenshot_path: Optional[str],
        job_id: Optional[int],
    ) -> int:
        now = time.time()
        with self._tx() as c:
            seq_row = c.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(seq_row[0])
            cur = c.execute(
                "INSERT INTO turns(session_id, seq, transcript, screenshot_path, final_text, "
                "duration_ms, started_at, ended_at, is_error, job_id) "
                "VALUES (?, ?, ?, ?, '', 0, ?, 0, 0, ?)",
                (session_id, seq, transcript, screenshot_path, now, job_id),
            )
            turn_id = int(cur.lastrowid or 0)
            c.execute(
                "UPDATE sessions SET last_activity_at = ?, status = 'running' WHERE id = ?",
                (now, session_id),
            )
        return turn_id

    def update_turn(self, turn_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as c:
            fts_sid: Optional[int] = None
            fts_old: Optional[tuple] = None
            if self._fts_enabled and "final_text" in fields:
                # Capture the currently-indexed values BEFORE the UPDATE so the
                # FTS 'delete' removes exactly what was inserted (A2).
                r = c.execute("SELECT session_id FROM turns WHERE id = ?", (turn_id,)).fetchone()
                if r is not None:
                    fts_sid = int(r["session_id"])
                    fts_old = self._fts_values(c, fts_sid)
            c.execute(f"UPDATE turns SET {cols} WHERE id = ?", (*fields.values(), turn_id))
            # If the turn just ended (final_text or duration set), nudge sessions.
            if "final_text" in fields or "ended_at" in fields:
                row = c.execute("SELECT session_id FROM turns WHERE id = ?", (turn_id,)).fetchone()
                if row is not None:
                    sid = int(row["session_id"])
                    new_status = "error" if fields.get("is_error") else "idle"
                    c.execute(
                        "UPDATE sessions SET last_activity_at = ?, status = ? WHERE id = ?",
                        (time.time(), new_status, sid),
                    )
            if fts_sid is not None and fts_old is not None:
                self._fts_delete(c, fts_sid, fts_old)
                new_vals = self._fts_values(c, fts_sid)
                if new_vals is not None:
                    self._fts_insert(c, fts_sid, new_vals)

    def add_tool_call(
        self, *, turn_id: int, name: str, input_json: str, started_at: Optional[float] = None
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO tool_calls(turn_id, name, input_json, result_json, is_error, "
                "started_at, ended_at) VALUES (?, ?, ?, '', 0, ?, 0)",
                (turn_id, name, input_json, started_at or time.time()),
            )
            return int(cur.lastrowid or 0)

    def update_tool_call(self, tool_call_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as c:
            c.execute(
                f"UPDATE tool_calls SET {cols} WHERE id = ?",
                (*fields.values(), tool_call_id),
            )

    def set_title(self, session_id: int, title: str, *, auto: bool = False) -> None:
        """Update a session title.

        auto=True means this came from the AutoTitler — keeps `auto_titled=1`
        so rolling re-title can keep refreshing it. auto=False (default) means
        a user rename — clears the flag so re-title leaves it alone.
        """
        with self._tx() as c:
            old = self._fts_values(c, session_id) if self._fts_enabled else None
            c.execute(
                "UPDATE sessions SET title = ?, auto_titled = ? WHERE id = ?",
                (title, 1 if auto else 0, session_id),
            )
            if old is not None:
                # Delete with the exact old (title, transcript, final_text) and
                # re-insert with the new title AND the aggregated final_text,
                # so the old title stops matching and final_text stays (A1).
                self._fts_delete(c, session_id, old)
                self._fts_insert(c, session_id, (title, old[1], old[2]))

    def set_notes(self, session_id: int, notes: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE sessions SET notes = ? WHERE id = ?", (notes, session_id))

    def pin(self, session_id: int, pinned: bool) -> None:
        with self._tx() as c:
            c.execute("UPDATE sessions SET pinned = ? WHERE id = ?", (1 if pinned else 0, session_id))

    def archive(self, session_id: int, archived: bool) -> None:
        with self._tx() as c:
            c.execute("UPDATE sessions SET archived = ? WHERE id = ?", (1 if archived else 0, session_id))

    # --- superintendent evidence ledger ---
    #
    # Single-writer spine: the operator INSERTs (via add_evidence) and the
    # SuperintendentDispatcher is the sole mutator of `status` thereafter.
    # No other subsystem writes this table, so counts are authoritative.

    SUPERINTENDENT_INBOX_PROFILE = "superintendent-inbox"

    def ensure_superintendent_inbox(self) -> int:
        """Find-or-create the pinned Superintendent evidence-inbox session.

        Identified by the sentinel profile value (never submitted to Claude —
        the dispatch layer intercepts PTTs routed here). Returns its id.
        """
        c = self._conn()
        row = c.execute(
            "SELECT id FROM sessions WHERE profile = ? ORDER BY id ASC LIMIT 1",
            (self.SUPERINTENDENT_INBOX_PROFILE,),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        sid = self.create_session(
            model="",
            transcript="",
            screenshot_path=None,
            status="idle",
            title="Superintendent — evidence inbox",
            profile=self.SUPERINTENDENT_INBOX_PROFILE,
        )
        self.pin(sid, True)
        return sid

    def is_superintendent_inbox(self, session_id: Optional[int]) -> bool:
        if not isinstance(session_id, int):
            return False
        row = self._conn().execute(
            "SELECT profile FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return bool(row) and row["profile"] == self.SUPERINTENDENT_INBOX_PROFILE

    def add_evidence(
        self,
        *,
        transcript: str,
        screenshot_path: Optional[str],
        inbox_session_id: Optional[int],
        inbox_turn_id: Optional[int],
        mode: str = "deploy",
        project: str = "",
    ) -> int:
        now = time.time()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO superintendent_evidence("
                "created_at, updated_at, status, transcript, screenshot_path, "
                "inbox_session_id, inbox_turn_id, mode, project) "
                "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                (now, now, transcript, screenshot_path, inbox_session_id, inbox_turn_id,
                 mode or "deploy", project or ""),
            )
            return int(cur.lastrowid or 0)

    def add_evidence_multi(
        self,
        *,
        transcript: str,
        screenshot_paths: List[str],
        inbox_session_id: Optional[int],
        inbox_turn_ids: Optional[List[int]] = None,
        mode: str = "deploy",
        project: str = "",
    ) -> int:
        """Insert one evidence row carrying multiple chained screenshots.

        The first shot is stored in `screenshot_path` (back-compat) and the full
        ordered list in `screenshot_paths` (JSON). `inbox_turn_id` records the
        first contributing turn for lineage. `mode` selects the worker lane
        ('deploy' remediation vs 'investigation' IR). `project` names the
        configured project the worker targets ('' = the profile default).
        """
        now = time.time()
        shots = [s for s in (screenshot_paths or []) if s]
        primary = shots[0] if shots else None
        paths_json = json.dumps(shots) if shots else None
        first_turn = (inbox_turn_ids or [None])[0]
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO superintendent_evidence("
                "created_at, updated_at, status, transcript, screenshot_path, "
                "screenshot_paths, inbox_session_id, inbox_turn_id, mode, project) "
                "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                (now, now, transcript, primary, paths_json, inbox_session_id, first_turn,
                 mode or "deploy", project or ""),
            )
            return int(cur.lastrowid or 0)

    def claim_next_queued_evidence(self) -> Optional[sqlite3.Row]:
        """Atomically claim the oldest queued item (queued -> dispatching).

        Single-flight by construction: only the dispatcher calls this, and the
        UPDATE...WHERE status='queued' guard means a racing second caller gets
        rowcount 0 and we return None.
        """
        now = time.time()
        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM superintendent_evidence WHERE status = 'queued' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = c.execute(
                "UPDATE superintendent_evidence SET status = 'dispatching', "
                "updated_at = ? WHERE id = ? AND status = 'queued'",
                (now, int(row["id"])),
            )
            if (cur.rowcount or 0) == 0:
                return None
            return c.execute(
                "SELECT * FROM superintendent_evidence WHERE id = ?",
                (int(row["id"]),),
            ).fetchone()

    def coalesce_siblings(self, primary_id: int, *, window_s: float) -> int:
        """Fold queued sibling evidence from the same inbox+mode burst into an
        already-claimed primary row, so one rapid chain becomes ONE worker.

        A straggler dump — e.g. a trailing PTT that finishes transcribing just
        after the operator files a chain — otherwise lands as its own evidence
        row and spawns a second, redundant session. This absorbs any queued
        sibling that is (a) from the same inbox_session_id, (b) in the same lane
        (mode), and (c) created within `window_s` of the growing burst frontier,
        so a steady stream of dumps a few seconds apart all coalesce while a
        deliberate new investigation minutes later does not. Absorbed rows are
        marked 'merged' (terminal, never dispatched) and their shots/transcripts
        are folded onto the primary. Returns the count absorbed.
        """
        if window_s <= 0:
            return 0
        now = time.time()

        def _shots(row) -> List[str]:
            raw = row["screenshot_paths"]
            if raw:
                try:
                    return [s for s in json.loads(raw) if s]
                except Exception:
                    pass
            return [row["screenshot_path"]] if row["screenshot_path"] else []

        restamp: List[int] = []
        with self._tx() as c:
            prow = c.execute(
                "SELECT * FROM superintendent_evidence WHERE id = ?", (primary_id,)
            ).fetchone()
            if prow is None or prow["inbox_session_id"] is None:
                return 0  # no inbox scope -> can't define a burst; leave as-is
            inbox = prow["inbox_session_id"]
            mode = prow["mode"] or "deploy"
            cands = c.execute(
                "SELECT * FROM superintendent_evidence "
                "WHERE status = 'queued' AND inbox_session_id = ? AND id != ? "
                "ORDER BY created_at ASC, id ASC",
                (inbox, primary_id),
            ).fetchall()

            # Select the contiguous same-lane burst that trails the primary.
            frontier = float(prow["created_at"] or now)
            absorbed: List[sqlite3.Row] = []
            for r in cands:
                # Same lane only — never merge a deploy fix with an investigation.
                if (r["mode"] or "deploy") != mode:
                    continue
                # Same project only — one worker works in exactly one repo.
                if (r["project"] or "") != (prow["project"] or ""):
                    continue
                created = float(r["created_at"] or 0)
                if created - frontier > window_s:
                    break  # rows are time-ordered; the burst has ended
                frontier = max(frontier, created)
                absorbed.append(r)
            if not absorbed:
                return 0
            # Mark first, then fold only rows that actually flipped 'queued' ->
            # 'merged', so a row that lost a claim race never gets double-ingested.
            merged_rows: List[sqlite3.Row] = []
            for r in absorbed:
                upd = c.execute(
                    "UPDATE superintendent_evidence SET status = 'merged', "
                    "finished_at = ?, updated_at = ? WHERE id = ? AND status = 'queued'",
                    (now, now, int(r["id"])),
                )
                if not (upd.rowcount or 0):
                    continue  # lost a race for this row; leave it queued
                merged_rows.append(r)
                if r["inbox_turn_id"] is not None:
                    restamp.append(int(r["inbox_turn_id"]))
            merged_count = len(merged_rows)
            if merged_count == 0:
                return 0
            shots = _shots(prow)
            transcripts = [prow["transcript"]] if (prow["transcript"] or "").strip() else []
            for r in merged_rows:
                shots.extend(_shots(r))
                if (r["transcript"] or "").strip():
                    transcripts.append(r["transcript"])
            c.execute(
                "UPDATE superintendent_evidence SET transcript = ?, screenshot_path = ?, "
                "screenshot_paths = ?, updated_at = ? WHERE id = ?",
                (
                    "\n".join(t for t in transcripts if t and t.strip()),
                    shots[0] if shots else None,
                    json.dumps(shots) if shots else None,
                    now, primary_id,
                ),
            )
        # Restamp the absorbed inbox turns outside the write txn (update_turn
        # opens its own) so the operator sees the straggler folded in, not a
        # phantom "captured -> queued" for a session that never spawns.
        for tid in restamp:
            try:
                self.update_turn(
                    tid, ended_at=now,
                    final_text=f"Merged into evidence #{primary_id} (same chain).",
                )
            except Exception:
                pass
        return merged_count

    def set_evidence_status(
        self,
        evidence_id: int,
        status: str,
        *,
        dispatch_session_id: Optional[int] = None,
        worktree_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        now = time.time()
        sets = ["status = ?", "updated_at = ?"]
        params: list = [status, now]
        if dispatch_session_id is not None:
            sets.append("dispatch_session_id = ?")
            params.append(dispatch_session_id)
        if worktree_path is not None:
            sets.append("worktree_path = ?")
            params.append(worktree_path)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if status == "working":
            sets.append("dispatched_at = ?")
            params.append(now)
        if status in ("deployed", "failed", "reported", "mr_opened"):
            sets.append("finished_at = ?")
            params.append(now)
        params.append(evidence_id)
        with self._tx() as c:
            c.execute(
                f"UPDATE superintendent_evidence SET {', '.join(sets)} WHERE id = ?",
                params,
            )

    def get_evidence(self, evidence_id: int) -> Optional[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM superintendent_evidence WHERE id = ?", (evidence_id,)
        ).fetchone()

    def list_evidence(
        self, *, statuses: Optional[List[str]] = None, limit: Optional[int] = None
    ) -> List[sqlite3.Row]:
        sql = "SELECT * FROM superintendent_evidence"
        params: list = []
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            sql += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._conn().execute(sql, params).fetchall()

    def active_evidence(self) -> List[sqlite3.Row]:
        """Rows currently occupying a worker slot (dispatching or working)."""
        return self._conn().execute(
            "SELECT * FROM superintendent_evidence "
            "WHERE status IN ('dispatching', 'working') ORDER BY id ASC"
        ).fetchall()

    def count_evidence_by_status(self) -> Dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM superintendent_evidence GROUP BY status"
        ).fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def set_worktree(
        self, session_id: int, *, path: str, branch: str, state: str,
    ) -> None:
        """Stamp a session's worktree fields atomically.

        state values: 'none' | 'active' | 'merged' | 'orphaned'. Caller is
        responsible for keeping the trio coherent — e.g. when releasing, set
        path='' and branch='' alongside state='merged' or 'orphaned'.
        """
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET worktree_path = ?, worktree_branch = ?, "
                "worktree_state = ? WHERE id = ?",
                (path, branch, state, session_id),
            )

    def list_sessions_with_active_worktree(self) -> List[Session]:
        """Every session whose DB row claims an 'active' worktree.

        Used by WorktreeManager.recover_on_startup() to reconcile DB state
        against `git worktree list --porcelain`. Anything in this list whose
        worktree dir is missing on disk gets moved to 'orphaned'.
        """
        c = self._conn()
        rows = c.execute(
            "SELECT * FROM sessions WHERE worktree_state = 'active' "
            "ORDER BY last_activity_at DESC"
        ).fetchall()
        return [Session.from_row(r) for r in rows]

    def mark_reviewed(self, session_id: int, reviewed: bool) -> None:
        """Set/clear the Done (reviewed) flag. reviewed_at is set to now when
        marking; nulled when clearing."""
        with self._tx() as c:
            if reviewed:
                c.execute(
                    "UPDATE sessions SET reviewed_at = ? WHERE id = ?",
                    (time.time(), session_id),
                )
            else:
                c.execute(
                    "UPDATE sessions SET reviewed_at = NULL WHERE id = ?",
                    (session_id,),
                )

    def bump_title_attempt(self, session_id: int) -> None:
        """Record one auto-title attempt (whether it succeeded or failed).
        Used by the AutoTitler to enforce its max-retries budget."""
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET title_attempts = title_attempts + 1, "
                "title_last_attempt_at = ? WHERE id = ?",
                (time.time(), session_id),
            )

    def reset_title_attempts(self, session_id: int) -> None:
        """Zero the auto-title attempt counter so the AutoTitler will retry on
        this session even if it previously hit the lifetime cap. Used by the
        right-click "Regenerate title" action."""
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET title_attempts = 0, title_last_attempt_at = 0 "
                "WHERE id = ?",
                (session_id,),
            )

    def list_placeholder_sessions(self, max_attempts: int = 5) -> List[Session]:
        """Sessions still showing the transcript-head placeholder title that
        haven't burned through their retry budget. Used by the launch-time
        backfill to retroactively name sessions left stranded by past failures.

        Candidate set is filtered server-side on cheap predicates; the exact
        "is the title still the placeholder?" check is done in Python so it
        matches `create_session`'s default rule (strip + first line + first 60)
        verbatim without re-encoding it in SQL."""
        c = self._conn()
        rows = c.execute(
            "SELECT * FROM sessions "
            "WHERE auto_titled = 1 "
            "  AND title_attempts < ? "
            "  AND archived = 0 "
            "  AND length(transcript) > 0 "
            "ORDER BY last_activity_at DESC",
            (max_attempts,),
        ).fetchall()
        out: List[Session] = []
        for r in rows:
            sess = Session.from_row(r)
            head = (sess.transcript or "").strip().split("\n", 1)[0][:60]
            if head and sess.title.startswith(head):
                out.append(sess)
        return out

    def delete(self, session_id: int) -> None:
        with self._tx() as c:
            if self._fts_enabled:
                # sessions_fts is a *contentless* FTS5 table (content=''), so a
                # row is removed via the special 'delete' command with its
                # indexed column values — `DELETE FROM sessions_fts` raises
                # "cannot DELETE from contentless fts5 table". Read the values
                # (incl. aggregated turn final_text) BEFORE the cascade drops
                # the turns.
                vals = self._fts_values(c, session_id)
                if vals is not None:
                    self._fts_delete(c, session_id, vals)
            c.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def auto_archive_older_than(self, cutoff_epoch: float) -> int:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE sessions SET archived = 1 "
                "WHERE last_activity_at < ? AND pinned = 0 AND archived = 0",
                (cutoff_epoch,),
            )
            return cur.rowcount or 0

    def list_reviewed_purgeable(self, cutoff_epoch: float) -> List[Session]:
        """Sessions eligible for the rolling retention purge: marked reviewed
        (Done) before `cutoff_epoch`, not pinned, and not currently running.

        Pinned rows are exempt (operator-anchored, same as auto-archive); a
        'running' row is never purged even if a stale reviewed_at lingers.
        Archived rows ARE included — an archived+reviewed session is a prime
        purge candidate. This is a read; the caller deletes on-disk artifacts
        then calls `delete()` per row (see holler/retention.py)."""
        rows = self._conn().execute(
            "SELECT * FROM sessions "
            "WHERE reviewed_at IS NOT NULL AND reviewed_at < ? "
            "  AND pinned = 0 AND status != 'running' "
            "ORDER BY reviewed_at ASC",
            (cutoff_epoch,),
        ).fetchall()
        return [Session.from_row(r) for r in rows]

    def is_screenshot_referenced(self, path: str) -> bool:
        """True if any session, turn, or superintendent_evidence row still
        references `path` (screenshot_path, or inside the evidence
        screenshot_paths JSON array). Retention uses this after deleting a
        session's rows so it never unlinks a capture another row still shows.
        """
        if not path:
            return False
        c = self._conn()
        if c.execute(
            "SELECT 1 FROM sessions WHERE screenshot_path = ? LIMIT 1", (path,)
        ).fetchone():
            return True
        if c.execute(
            "SELECT 1 FROM turns WHERE screenshot_path = ? LIMIT 1", (path,)
        ).fetchone():
            return True
        has_evidence = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'superintendent_evidence'"
        ).fetchone()
        if has_evidence and c.execute(
            "SELECT 1 FROM superintendent_evidence WHERE screenshot_path = ? "
            "OR instr(COALESCE(screenshot_paths, ''), ?) > 0 LIMIT 1",
            (path, json.dumps(path)),
        ).fetchone():
            return True
        return False

    # --- reads ---

    def get_session(self, session_id: int) -> Optional[Session]:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return Session.from_row(row) if row else None

    def get_session_by_sdk_id(self, sdk_session_id: str) -> Optional[Session]:
        row = self._conn().execute(
            "SELECT * FROM sessions WHERE sdk_session_id = ?", (sdk_session_id,)
        ).fetchone()
        return Session.from_row(row) if row else None

    def list_sessions(
        self,
        *,
        include_archived: bool = False,
        pinned_only: bool = False,
        running_only: bool = False,
        reviewed_only: bool = False,
        exclude_reviewed: bool = False,
        limit: Optional[int] = None,
    ) -> List[Session]:
        wheres = []
        params: list = []
        if not include_archived:
            wheres.append("archived = 0")
        if pinned_only:
            wheres.append("pinned = 1")
        if running_only:
            wheres.append("status = 'running'")
        if reviewed_only:
            wheres.append("reviewed_at IS NOT NULL")
        if exclude_reviewed:
            # Pinned + running rows are always visible even if marked reviewed,
            # since they're explicitly user-anchored / live.
            wheres.append("(reviewed_at IS NULL OR pinned = 1 OR status = 'running')")
        sql = "SELECT * FROM sessions"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY pinned DESC, last_activity_at DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = self._conn().execute(sql, params).fetchall()
        return [Session.from_row(r) for r in rows]

    def get_turns(self, session_id: int) -> List[Turn]:
        rows = self._conn().execute(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY seq ASC", (session_id,)
        ).fetchall()
        return [Turn.from_row(r) for r in rows]

    def session_token_totals(self, session_ids: List[int]) -> dict:
        """Return {session_id: {fresh_in, ctx_in, output, cost}} aggregated from turns.

        fresh_in = SUM(input_tokens) — non-cached input only. Adding fresh_in +
        output gives "real tokens of new content moved through this session,"
        which is what an operator means by "tokens used."

        ctx_in = input + cache_creation + cache_read — kept for cost
        diagnostics, but inflated because the same conversation context is
        re-billed (mostly at cache-read rate) every turn.
        """
        out: dict = {
            sid: {"fresh_in": 0, "ctx_in": 0, "output": 0, "cost": 0.0}
            for sid in session_ids
        }
        if not session_ids:
            return out
        placeholders = ",".join("?" * len(session_ids))
        rows = self._conn().execute(
            f"SELECT session_id, "
            f"  COALESCE(SUM(input_tokens), 0) AS fresh_in, "
            f"  COALESCE(SUM(input_tokens + cache_creation_input_tokens + cache_read_input_tokens), 0) AS ctx_in, "
            f"  COALESCE(SUM(output_tokens), 0) AS output, "
            f"  COALESCE(SUM(total_cost_usd), 0.0) AS cost "
            f"FROM turns WHERE session_id IN ({placeholders}) GROUP BY session_id",
            tuple(session_ids),
        ).fetchall()
        for r in rows:
            out[int(r["session_id"])] = {
                "fresh_in": int(r["fresh_in"] or 0),
                "ctx_in": int(r["ctx_in"] or 0),
                "output": int(r["output"] or 0),
                "cost": float(r["cost"] or 0.0),
            }
        return out

    def get_tool_calls(self, turn_id: int) -> List[ToolCallRecord]:
        rows = self._conn().execute(
            "SELECT * FROM tool_calls WHERE turn_id = ? ORDER BY id ASC", (turn_id,)
        ).fetchall()
        return [ToolCallRecord.from_row(r) for r in rows]

    def get_tool_calls_for_turns(
        self, turn_ids: List[int]
    ) -> dict[int, List[ToolCallRecord]]:
        """Batch variant — one SELECT for every turn the caller cares about.

        Replaces the N+1 pattern of looping `get_tool_calls(turn_id)` over
        every turn of a session render. Returns an empty list for any turn
        with no tool calls, so callers can `.get(tid, [])` without surprises.
        """
        out: dict[int, List[ToolCallRecord]] = {tid: [] for tid in turn_ids}
        if not turn_ids:
            return out
        placeholders = ",".join("?" * len(turn_ids))
        rows = self._conn().execute(
            f"SELECT * FROM tool_calls WHERE turn_id IN ({placeholders}) "
            f"ORDER BY turn_id ASC, id ASC",
            tuple(turn_ids),
        ).fetchall()
        for r in rows:
            tid = int(r["turn_id"])
            out.setdefault(tid, []).append(ToolCallRecord.from_row(r))
        return out

    def search(self, query: str, *, include_archived: bool = False, limit: int = 50) -> List[Session]:
        q = (query or "").strip()
        if not q:
            return self.list_sessions(include_archived=include_archived, limit=limit)
        c = self._conn()
        if self._fts_enabled:
            tokens = [t for t in _split_safe(q) if t]
            if not tokens:
                return []
            fts_query = " OR ".join(f'"{t}"' for t in tokens)
            sql = (
                "SELECT s.*, bm25(sessions_fts) AS rank "
                "FROM sessions_fts JOIN sessions s ON s.id = sessions_fts.rowid "
                "WHERE sessions_fts MATCH ? "
            )
            params: list = [fts_query]
            if not include_archived:
                sql += "AND s.archived = 0 "
            sql += "ORDER BY rank ASC, s.last_activity_at DESC LIMIT ?"
            params.append(limit)
            rows = c.execute(sql, params).fetchall()
        else:
            like = f"%{q}%"
            sql = (
                "SELECT * FROM sessions "
                "WHERE (title LIKE ? OR transcript LIKE ?) "
            )
            params = [like, like]
            if not include_archived:
                sql += "AND archived = 0 "
            sql += "ORDER BY last_activity_at DESC LIMIT ?"
            params.append(limit)
            rows = c.execute(sql, params).fetchall()
        return [Session.from_row(r) for r in rows]


def _split_safe(q: str) -> list[str]:
    """Tokenize for FTS5 input — strip non-word chars, lowercase, drop very short tokens."""
    import re
    return [t for t in re.split(r"\W+", q.lower()) if len(t) >= 2]
