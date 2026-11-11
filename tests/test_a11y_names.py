"""Accessibility scan over every visual-scenario surface.

Builds each window / dialog the golden-image scenarios build (reusing
tests/visual/scenarios.py) and checks what a screen reader would announce:

  * every visible button has a name — its text or an accessible name (an
    icon- or glyph-only button with neither is announced as just "button");
  * every QListWidget row has accessible text — rows drawn with
    setItemWidget have no item text of their own, so without
    Qt.AccessibleTextRole they are announced as empty.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6 import QtCore, QtWidgets  # noqa: E402

# Import the real modules at collection time (test_sidebar_organization stubs
# PySide6 in sys.modules if they aren't loaded yet).
import holler.panel  # noqa: E402,F401
import holler.sessions_window  # noqa: E402,F401
from tests.visual import scenarios as sc  # noqa: E402


def _new_top_levels(env) -> list[QtWidgets.QWidget]:
    app = QtWidgets.QApplication.instance()
    return [w for w in app.topLevelWidgets() if id(w) not in env._before]


def _shown_in(widget: QtWidgets.QWidget, top: QtWidgets.QWidget) -> bool:
    return widget is top or widget.isVisibleTo(top)


def _describe(w: QtWidgets.QWidget) -> str:
    parent = w.parentWidget()
    where = type(parent).__name__ + (f"#{parent.objectName()}" if parent and parent.objectName() else "")
    return f"{type(w).__name__}#{w.objectName() or '?'} in {where} (tip={w.toolTip()!r})"


def scan(tops: list[QtWidgets.QWidget]) -> list[str]:
    """Problems found on `tops` (and their children), as readable strings."""
    problems: list[str] = []
    for top in tops:
        if not top.isVisible():
            continue
        for b in top.findChildren(QtWidgets.QAbstractButton):
            if not _shown_in(b, top):
                continue
            if not (b.text().strip() or b.accessibleName().strip()):
                problems.append(f"unnamed button: {_describe(b)}")
        for lst in top.findChildren(QtWidgets.QListWidget):
            if not _shown_in(lst, top):
                continue
            for i in range(lst.count()):
                it = lst.item(i)
                text = it.data(QtCore.Qt.AccessibleTextRole) or it.text()
                if not str(text or "").strip():
                    problems.append(f"list row {i} without accessible text in {_describe(lst)}")
    return problems


@pytest.mark.parametrize("name", sorted(sc.SCENARIOS))
def test_scenario_surfaces_are_named(name, app, tmp_path):
    with sc.VisualEnv(tmp_path / "env") as env:
        built = sc.SCENARIOS[name](env)
        sc.pump(30)
        tops = _new_top_levels(env)
        if isinstance(built, QtWidgets.QWidget) and built.window() not in tops:
            tops.append(built.window())
        problems = scan(tops)
    assert not problems, f"{name}:\n  " + "\n  ".join(problems)


def test_scan_flags_an_unnamed_icon_button_and_an_empty_row(app):
    """The scanner itself: it must catch the two failure modes."""
    w = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(w)
    icon_only = QtWidgets.QPushButton()
    lay.addWidget(icon_only)
    lst = QtWidgets.QListWidget()
    item = QtWidgets.QListWidgetItem()
    lst.addItem(item)
    lst.setItemWidget(item, QtWidgets.QLabel("drawn, not announced"))
    lay.addWidget(lst)
    w.show()
    try:
        assert len(scan([w])) == 2
        icon_only.setAccessibleName("Archive session")
        item.setData(QtCore.Qt.AccessibleTextRole, "Fix the login bug, Done")
        assert scan([w]) == []
    finally:
        w.close()
        w.deleteLater()
