"""Capability inventory — guards "never remove a capability" across UI rewrites.

Programmatically enumerates what the operator can *do* in holler's main
surfaces and compares it with tests/goldens/capabilities.json:

  * keyboard shortcuts (QShortcut / QAction key sequences) on SessionsWindow,
    with the widget they are scoped to;
  * every menu entry: SessionsWindow ⋯ header menu, the session-row context
    menu (all its pinned/reviewed/archived variants), the sort menu, and the
    Panel ⋯ menu — menus are built for real by the app code; only exec() is replaced;
  * every clickable button (text / tooltip / objectName) in SessionsWindow
    (incl. a session row's hover buttons), SettingsDialog, FirstRunDialog,
    Panel;
  * every Config field SettingsDialog can edit, with its form label;
    FirstRunDialog's editable fields;
  * the filter chips and sort options.

Entries are semantic strings (ids + visible text), so restyling never trips
this test but removing or renaming a capability does. On a disappearance the
test FAILS listing what vanished; additions pass with a printed notice.
Regenerate (after a deliberate, reviewed change) with:

    HOLLER_UPDATE_GOLDENS=1 .venv/bin/python -m pytest tests/test_capability_inventory.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

# Import the real modules at collection time (test_sidebar_organization stubs
# PySide6 in sys.modules if they aren't loaded yet).
import holler.panel  # noqa: E402,F401
import holler.sessions_window  # noqa: E402,F401
from tests.visual import scenarios as sc  # noqa: E402

SNAPSHOT = Path(__file__).resolve().parent / "goldens" / "capabilities.json"


def _update_requested() -> bool:
    return os.environ.get("HOLLER_UPDATE_GOLDENS", "") not in ("", "0", "false", "no")


# ── collectors ───────────────────────────────────────────────────────────────

def _attr_name(owner: QtCore.QObject, obj: QtCore.QObject) -> str:
    """Name of the attribute on `owner` (or its descendants' Python wrappers)
    that holds `obj` — a stable semantic handle for unnamed widgets."""
    for k, v in vars(owner).items():
        if v is obj:
            return k
    if obj is owner:
        return "<window>"
    name = obj.objectName()
    return name or type(obj).__name__


def _keyseq(seq: QtGui.QKeySequence) -> str:
    return seq.toString(QtGui.QKeySequence.PortableText)


def _shortcuts(prefix: str, root: QtWidgets.QWidget) -> set[str]:
    out = set()
    for sc_ in root.findChildren(QtGui.QShortcut):
        scope = sc_.parent()
        out.add(f"{prefix}:shortcut:{_keyseq(sc_.key())} @{_attr_name(root, scope)}")
    for act in root.findChildren(QtGui.QAction):
        for seq in act.shortcuts():
            out.add(f"{prefix}:action-shortcut:{_keyseq(seq)} {act.text()}")
    return out


# Buttons whose visible text is live data rather than a label: identify them
# by role so a model switch or sort change doesn't look like a removal.
_DYNAMIC_TEXT = {
    "_model_badge": "<model badge: click to cycle model>",
    "modelBadge": "<model badge: click to cycle model>",
    "_sort_btn": "<sort button>",
}
# Disabled status read-outs rendered as buttons — not capabilities.
_STATUS_ONLY = {"warmBadge"}


def _buttons(prefix: str, root: QtWidgets.QWidget) -> set[str]:
    out = set()
    for b in root.findChildren(QtWidgets.QAbstractButton):
        if isinstance(b, QtWidgets.QAbstractButton) and b.parent() is not None and isinstance(
            b.parent(), QtWidgets.QAbstractScrollArea
        ):
            continue  # scroll-area corner buttons etc.
        kind = type(b).__name__
        handle = _attr_name(root, b)
        text = b.text().strip()
        tip = b.toolTip().strip()
        if handle in _STATUS_ONLY or b.objectName() in _STATUS_ONLY:
            continue
        semantic = _DYNAMIC_TEXT.get(handle) or _DYNAMIC_TEXT.get(b.objectName())
        if semantic:
            # Text is live data (current model / sort) — key on the role.
            out.add(f"{prefix}:{kind}:{semantic}" + (f" — {tip}" if tip else ""))
            continue
        if not text and not tip:
            owner = b.parent()
            where = _attr_name(root, owner) if owner is not None else "?"
            out.add(f"{prefix}:{kind}:<unlabelled, in {where}>")
            continue
        out.add(f"{prefix}:{kind}:{text or tip}" + (f" — {tip}" if text and tip else ""))
    return out


_RealQMenu = QtWidgets.QMenu


class _MenuCapture:
    """Swap in a QMenu whose exec() records the actions of every menu built, choose none."""

    def __init__(self) -> None:
        self.menus: list[list[str]] = []

    def __enter__(self):
        cap = self

        class _RecordingMenu(_RealQMenu):
            def exec(self, *a, **k):  # noqa: A003 - Qt name
                cap.menus.append([a_.text() for a_ in self.actions() if not a_.isSeparator()])
                return None

        # Holler builds menus as `QtWidgets.QMenu(self)`; swapping the module
        # attribute is the only interception Shiboken honours (patching the
        # method on the C++-backed class is ignored).
        self._p = mock.patch.object(QtWidgets, "QMenu", _RecordingMenu)
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()


def _row_pos(w, sid: int) -> QtCore.QPoint:
    for i in range(w._list.count()):
        it = w._list.item(i)
        s = it.data(QtCore.Qt.UserRole)
        if s is not None and not it.data(QtCore.Qt.UserRole + 1) and s.id == sid:
            return w._list.visualItemRect(it).center()
    raise AssertionError(f"session {sid} not listed")


def collect_inventory(tmp_path: Path) -> list[str]:
    inv: set[str] = set()
    with sc.VisualEnv(tmp_path / "env") as env:
        # ---- SessionsWindow ----
        w, store, jobs, seed = sc.make_sessions_window(env)
        inv |= _shortcuts("sessions", w)
        inv |= _buttons("sessions", w)
        row = next(iter(w._row_widgets.values()))
        inv |= {e.replace("sessions:", "sessions-row:", 1) for e in _buttons("sessions", row)}

        for key, btn in w._chips.items():
            inv.add(f"sessions:filter-chip:{key} = {btn.text()}")

        with _MenuCapture() as cap:
            w._show_header_menu()
        inv |= {f"sessions:header-menu:{t}" for m in cap.menus for t in m}

        with _MenuCapture() as cap:
            w._show_sort_menu()
        inv |= {f"sessions:sort-option:{t.lstrip('• ').strip()}" for m in cap.menus for t in m}

        # Context menu across row states: plain, pinned, reviewed (Done view),
        # archived (Archived view).
        with _MenuCapture() as cap:
            for flt, sid in (("active", seed.rich), ("active", seed.pinned),
                             ("done", seed.done_recent), ("archived", seed.archived)):
                w._set_filter(flt)
                sc.pump(30)
                w._show_context_menu(_row_pos(w, sid))
        inv |= {f"sessions:row-menu:{t}" for m in cap.menus for t in m}

        # ---- Panel ----
        from holler.jobs import JobStore
        from holler.panel import Panel

        pjobs = JobStore(None)
        p = Panel(pjobs, model_label="OPUS 5")
        for j in sc.panel_jobs():
            pjobs.add(j)
        inv |= _shortcuts("panel", p)
        inv |= _buttons("panel", p)
        with _MenuCapture() as cap:
            p._show_menu()
        inv |= {f"panel:menu:{t}" for m in cap.menus for t in m}

        # ---- SettingsDialog ----
        from holler.settings_dialog import SettingsDialog

        d = SettingsDialog(sc.fixed_config(), on_save=lambda c, ch: None)
        inv |= _shortcuts("settings", d)
        inv |= _buttons("settings", d)
        form = d.findChild(QtWidgets.QFormLayout)
        for name, widget in d._fields.items():
            label = ""
            lw = form.labelForField(widget) if form is not None else None
            if lw is None and form is not None:
                # Row whose field is a layout containing the widget.
                for r in range(form.rowCount()):
                    item = form.itemAt(r, QtWidgets.QFormLayout.FieldRole)
                    lay = item.layout() if item is not None else None
                    if lay is not None and any(
                        lay.itemAt(i).widget() is widget for i in range(lay.count())
                    ):
                        lab = form.itemAt(r, QtWidgets.QFormLayout.LabelRole)
                        lw = lab.widget() if lab is not None else None
                        break
            if isinstance(lw, QtWidgets.QLabel):
                label = lw.text()
            inv.add(f"settings:config-field:{name} [{type(widget).__name__}] {label}".rstrip())
        d.reject()

        # ---- FirstRunDialog ----
        from holler.first_run import FirstRunDialog

        fr = FirstRunDialog(sc.fixed_config())
        inv |= _shortcuts("first-run", fr)
        inv |= _buttons("first-run", fr)
        for attr in ("_cwd_edit", "_hotkey_edit", "_model"):
            if hasattr(fr, attr):
                inv.add(f"first-run:field:{attr} [{type(getattr(fr, attr)).__name__}]")
        fr.reject()
    return sorted(inv)


# ── the test ─────────────────────────────────────────────────────────────────

def test_capability_inventory(app, tmp_path):
    current = collect_inventory(tmp_path)
    assert len(current) > 40, f"inventory suspiciously small: {current}"

    if _update_requested() or not SNAPSHOT.exists():
        if not _update_requested():
            pytest.fail(f"missing {SNAPSHOT} — run with HOLLER_UPDATE_GOLDENS=1 to create it")
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps({"capabilities": current}, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        print(f"wrote {len(current)} capabilities to {SNAPSHOT}")
        return

    previous = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["capabilities"]
    missing = sorted(set(previous) - set(current))
    added = sorted(set(current) - set(previous))
    if added:
        print("NOTICE: new capabilities (not yet in the snapshot — run with "
              "HOLLER_UPDATE_GOLDENS=1 to record them):\n  " + "\n  ".join(added))
    assert not missing, (
        "Capabilities DISAPPEARED from the UI (removed or renamed):\n  "
        + "\n  ".join(missing)
        + "\n\nIf this is a deliberate, reviewed change, regenerate with "
          "HOLLER_UPDATE_GOLDENS=1 and commit tests/goldens/capabilities.json."
        + (("\n\nPossibly renamed to one of:\n  " + "\n  ".join(added)) if added else "")
    )
