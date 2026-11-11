"""Unit tests for the sidebar reorganization + rolling re-title work.

Covers:
  - Store migration v4 lands the auto_titled column with the right default
  - set_title flag flips auto_titled correctly (auto=True keeps it,
    auto=False clears it — distinguishes AutoTitler writes from user renames)
  - should_retitle threshold + debounce logic
  - _age_bucket boundaries
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

# Stub PySide6 so importing sessions_window for _age_bucket works headlessly.
# Only when the real thing is unavailable — installing stubs over an
# importable PySide6 poisons sys.modules for every Qt test collected after us.
try:
    import PySide6.QtWidgets  # noqa: F401
except Exception:
    pass
if "PySide6" not in sys.modules:
    pyside = types.ModuleType("PySide6")
    qtcore = types.ModuleType("PySide6.QtCore")
    qtgui = types.ModuleType("PySide6.QtGui")
    qtwidgets = types.ModuleType("PySide6.QtWidgets")

    class _Sig:
        def __init__(self, *a, **kw):
            self._slots = []
        def connect(self, slot):
            self._slots.append(slot)
        def emit(self, *a, **kw):
            for s in list(self._slots):
                s(*a, **kw)

    class _QObject:
        def __init__(self, *a, **kw):
            pass

    class _Stub:
        def __init__(self, *a, **kw):
            pass
        def __getattr__(self, name):
            return _Stub()
        def __call__(self, *a, **kw):
            return _Stub()

    qtcore.Signal = lambda *a, **kw: _Sig()
    qtcore.QObject = _QObject
    qtcore.Slot = lambda *a, **kw: (lambda fn: fn)
    qtcore.Qt = _Stub()
    qtcore.QSize = _Stub
    qtcore.QPoint = _Stub
    qtcore.QRect = _Stub
    qtcore.QTimer = _Stub
    qtgui.QShortcut = _Stub
    qtgui.QKeySequence = _Stub
    qtgui.QCloseEvent = _Stub
    qtwidgets.QApplication = _Stub
    qtwidgets.QFrame = _Stub
    qtwidgets.QMainWindow = _Stub
    qtwidgets.QWidget = _Stub
    qtwidgets.QPushButton = _Stub
    qtwidgets.QLabel = _Stub
    qtwidgets.QLineEdit = _Stub
    qtwidgets.QListWidget = _Stub
    qtwidgets.QListWidgetItem = _Stub
    qtwidgets.QHBoxLayout = _Stub
    qtwidgets.QVBoxLayout = _Stub
    qtwidgets.QSplitter = _Stub
    qtwidgets.QMenu = _Stub
    qtwidgets.QInputDialog = _Stub
    qtwidgets.QMessageBox = _Stub
    qtwidgets.QSizePolicy = _Stub
    qtwidgets.QAbstractItemView = _Stub
    qtwidgets.QDialog = _Stub
    qtwidgets.QDialogButtonBox = _Stub
    qtwidgets.QComboBox = _Stub
    qtwidgets.QFormLayout = _Stub
    qtwidgets.QTextEdit = _Stub
    qtwidgets.QCheckBox = _Stub
    qtwidgets.QSpinBox = _Stub
    qtwidgets.QRadioButton = _Stub
    qtwidgets.QButtonGroup = _Stub
    qtwidgets.QGroupBox = _Stub
    qtwidgets.QStackedWidget = _Stub
    qtwidgets.QScrollArea = _Stub
    qtwidgets.QSpacerItem = _Stub
    qtwidgets.QTabWidget = _Stub
    qtwidgets.QToolButton = _Stub
    pyside.QtCore = qtcore
    pyside.QtGui = qtgui
    pyside.QtWidgets = qtwidgets
    sys.modules["PySide6"] = pyside
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtGui"] = qtgui
    sys.modules["PySide6.QtWidgets"] = qtwidgets

# Stub claude_agent_sdk so auto_title imports clean (again: only if missing).
try:
    import claude_agent_sdk  # noqa: F401
except Exception:
    pass
if "claude_agent_sdk" not in sys.modules:
    sdk = types.ModuleType("claude_agent_sdk")
    for name in (
        "AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
        "RateLimitEvent", "ResultMessage", "SystemMessage", "TextBlock",
        "ThinkingBlock", "ToolUseBlock", "UserMessage",
    ):
        setattr(sdk, name, type(name, (), {}))
    sys.modules["claude_agent_sdk"] = sdk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from holler.auto_title import (  # noqa: E402
    RETITLE_TURN_THRESHOLDS,
    _local_heuristic_title,
    is_placeholder_title,
    should_retitle,
)
from holler.store import Store  # noqa: E402


# ── Store auto_titled migration + set_title flag ────────────────────────────


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "holler.db")
    yield db
    db.close()


def test_create_session_defaults_to_auto_titled(store):
    sid = store.create_session(model="opus", transcript="hello world", screenshot_path=None)
    sess = store.get_session(sid)
    assert sess is not None
    assert sess.auto_titled is True


def test_set_title_default_is_user_rename_clears_flag(store):
    sid = store.create_session(model="opus", transcript="hello", screenshot_path=None)
    assert store.get_session(sid).auto_titled is True
    store.set_title(sid, "User picked this")
    sess = store.get_session(sid)
    assert sess.title == "User picked this"
    assert sess.auto_titled is False


def test_set_title_auto_true_preserves_flag(store):
    sid = store.create_session(model="opus", transcript="hello", screenshot_path=None)
    store.set_title(sid, "Auto-generated title", auto=True)
    sess = store.get_session(sid)
    assert sess.title == "Auto-generated title"
    assert sess.auto_titled is True


def test_set_title_auto_then_user_clears_flag(store):
    sid = store.create_session(model="opus", transcript="hello", screenshot_path=None)
    store.set_title(sid, "Auto first", auto=True)
    assert store.get_session(sid).auto_titled is True
    store.set_title(sid, "User override")  # default auto=False
    assert store.get_session(sid).auto_titled is False


def test_set_title_user_then_auto_can_restore_flag(store):
    """Edge case: if a user later wants the auto-titler back on (e.g. via
    some future "reset to auto" action), set_title with auto=True must
    successfully re-flip the flag. Today no UI surfaces this, but the
    Store API has to honor it."""
    sid = store.create_session(model="opus", transcript="hello", screenshot_path=None)
    store.set_title(sid, "User picked")  # clears flag
    assert store.get_session(sid).auto_titled is False
    store.set_title(sid, "Back to auto", auto=True)
    assert store.get_session(sid).auto_titled is True


# ── should_retitle: threshold + debounce ────────────────────────────────────


def test_should_retitle_thresholds_match_constant():
    """Sanity: RETITLE_TURN_THRESHOLDS is the source of truth."""
    assert RETITLE_TURN_THRESHOLDS == frozenset({3, 6, 12, 24, 48, 96})


@pytest.mark.parametrize("turn_count", [3, 6, 12, 24, 48, 96])
def test_should_retitle_fires_at_thresholds(turn_count):
    # last_retitle_at = 0 (never retitled) and now is well past the debounce.
    assert should_retitle(turn_count, last_retitle_at=0.0, now=1_000_000.0) is True


@pytest.mark.parametrize("turn_count", [1, 2, 4, 5, 7, 11, 13, 23, 25, 47, 49, 95, 97, 200])
def test_should_retitle_skips_off_threshold(turn_count):
    assert should_retitle(turn_count, last_retitle_at=0.0, now=1_000_000.0) is False


def test_should_retitle_debounces_within_60s():
    now = 1_000_000.0
    # Last retitle 30s ago at a threshold turn — still inside the 60s window.
    assert should_retitle(6, last_retitle_at=now - 30, now=now) is False


def test_should_retitle_fires_after_debounce():
    now = 1_000_000.0
    assert should_retitle(6, last_retitle_at=now - 61, now=now) is True


def test_should_retitle_custom_debounce():
    now = 1_000_000.0
    assert should_retitle(6, last_retitle_at=now - 5, now=now, debounce_s=10) is False
    assert should_retitle(6, last_retitle_at=now - 11, now=now, debounce_s=10) is True


# ── _age_bucket ─────────────────────────────────────────────────────────────


def test_age_bucket_boundaries():
    # Import here so the PySide stub (set up above) is in place.
    from holler.sessions_window import _age_bucket
    now = 1_000_000.0

    # < 24h
    assert _age_bucket(now - 60, now=now) == "TODAY"
    assert _age_bucket(now - (23 * 3600 + 59 * 60), now=now) == "TODAY"
    # 24h ≤ x < 48h
    assert _age_bucket(now - 24 * 3600, now=now) == "YESTERDAY"
    assert _age_bucket(now - (47 * 3600), now=now) == "YESTERDAY"
    # 48h ≤ x < 7d
    assert _age_bucket(now - 48 * 3600, now=now) == "THIS WEEK"
    assert _age_bucket(now - (6 * 86400 + 12 * 3600), now=now) == "THIS WEEK"
    # 7d ≤ x < 30d
    assert _age_bucket(now - 7 * 86400, now=now) == "THIS MONTH"
    assert _age_bucket(now - (29 * 86400), now=now) == "THIS MONTH"
    # ≥ 30d
    assert _age_bucket(now - 30 * 86400, now=now) == "OLDER"
    assert _age_bucket(now - 365 * 86400, now=now) == "OLDER"


def test_age_bucket_clamps_future_timestamps_to_today():
    """If clock skew leaves last_activity_at in the future, treat as TODAY."""
    from holler.sessions_window import _age_bucket
    now = 1_000_000.0
    assert _age_bucket(now + 3600, now=now) == "TODAY"


# ── _local_heuristic_title ──────────────────────────────────────────────────


def test_local_heuristic_strips_leading_filler():
    """'I want to fix the posture score calculation' should drop 'I want to'
    and title-case 'Fix The Posture Score Calculation'."""
    out = _local_heuristic_title("I want to fix the posture score calculation")
    # First meaningful word ('fix') survives, capitalized.
    assert out.split()[0] == "Fix"
    # Filler stripped — 'I', 'want', 'to' should not appear.
    assert "want" not in out.lower().split()
    # 7-word cap.
    assert len(out.split()) <= 7


def test_local_heuristic_uppercases_acronyms():
    out = _local_heuristic_title("Can you fix the UI for the API integration")
    # 'UI' and 'API' should stay uppercase (filler-stripped 'Fix Ui For The Api…'
    # would be wrong without the acronym table).
    assert "UI" in out.split()
    assert "API" in out.split()


def test_local_heuristic_handles_only_filler():
    """All-filler input falls back to the original tokens so we never return
    blank."""
    out = _local_heuristic_title("can you please")
    assert out  # non-empty
    assert len(out.split()) <= 7


def test_local_heuristic_empty_input():
    assert _local_heuristic_title("") == ""
    assert _local_heuristic_title("   \n  ") == ""


def test_local_heuristic_caps_word_count():
    """A long transcript should be capped at 7 meaningful words."""
    long = "we should refactor the authentication middleware and audit all the JWT validation paths and then ship"
    out = _local_heuristic_title(long)
    assert len(out.split()) <= 7


# ── is_placeholder_title ────────────────────────────────────────────────────


def test_is_placeholder_title_true_for_transcript_head(store):
    sid = store.create_session(
        model="opus",
        transcript="this is what the user said",
        screenshot_path=None,
    )
    sess = store.get_session(sid)
    assert sess is not None
    assert is_placeholder_title(sess) is True


def test_is_placeholder_title_false_after_ai_titled(store):
    sid = store.create_session(
        model="opus",
        transcript="this is what the user said",
        screenshot_path=None,
    )
    store.set_title(sid, "Refactor Posture Score Engine", auto=True)
    sess = store.get_session(sid)
    assert is_placeholder_title(sess) is False


def test_is_placeholder_title_false_after_user_rename(store):
    sid = store.create_session(
        model="opus",
        transcript="this is what the user said",
        screenshot_path=None,
    )
    # User rename → auto_titled = 0 → can't be a placeholder.
    store.set_title(sid, "this is what the user said", auto=False)
    sess = store.get_session(sid)
    assert is_placeholder_title(sess) is False


# ── Store: title attempt tracking + backfill query ──────────────────────────


def test_bump_title_attempt_increments(store):
    sid = store.create_session(model="opus", transcript="hi", screenshot_path=None)
    s0 = store.get_session(sid)
    assert s0.title_attempts == 0
    assert s0.title_last_attempt_at == 0.0
    store.bump_title_attempt(sid)
    s1 = store.get_session(sid)
    assert s1.title_attempts == 1
    assert s1.title_last_attempt_at > 0


def test_reset_title_attempts_zeros_counter(store):
    sid = store.create_session(model="opus", transcript="hi", screenshot_path=None)
    store.bump_title_attempt(sid)
    store.bump_title_attempt(sid)
    assert store.get_session(sid).title_attempts == 2
    store.reset_title_attempts(sid)
    s = store.get_session(sid)
    assert s.title_attempts == 0
    assert s.title_last_attempt_at == 0.0


def test_list_placeholder_sessions_includes_default_title(store):
    """A freshly-created session has a transcript-head placeholder; backfill
    should pick it up."""
    sid = store.create_session(
        model="opus",
        transcript="please refactor the auth middleware",
        screenshot_path=None,
    )
    placeholders = store.list_placeholder_sessions()
    assert any(s.id == sid for s in placeholders)


def test_list_placeholder_sessions_excludes_ai_titled(store):
    sid = store.create_session(
        model="opus",
        transcript="please refactor the auth middleware",
        screenshot_path=None,
    )
    store.set_title(sid, "Refactor Auth Middleware Layer", auto=True)
    placeholders = store.list_placeholder_sessions()
    assert not any(s.id == sid for s in placeholders)


def test_list_placeholder_sessions_respects_attempt_cap(store):
    sid = store.create_session(
        model="opus",
        transcript="please refactor the auth middleware",
        screenshot_path=None,
    )
    # Burn through the retry budget — backfill should leave this one alone.
    for _ in range(5):
        store.bump_title_attempt(sid)
    placeholders = store.list_placeholder_sessions(max_attempts=5)
    assert not any(s.id == sid for s in placeholders)
    # With a higher cap it reappears.
    placeholders = store.list_placeholder_sessions(max_attempts=10)
    assert any(s.id == sid for s in placeholders)


def test_list_placeholder_sessions_excludes_archived(store):
    sid = store.create_session(
        model="opus",
        transcript="please refactor the auth middleware",
        screenshot_path=None,
    )
    store.archive(sid, True)
    placeholders = store.list_placeholder_sessions()
    assert not any(s.id == sid for s in placeholders)
