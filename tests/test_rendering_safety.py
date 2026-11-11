"""Regression tests for Group D rendering fixes (detail view, panel, markdown).

Runs real offscreen Qt widgets via the session ``app`` fixture in conftest.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from holler import detail as detail_mod
from holler import markdown_render
from holler.detail import DetailView, _compact_tool_result_lines
from holler.jobs import Job


# ---------------------------------------------------------------- D1 links

@pytest.fixture
def opened(monkeypatch):
    calls: list[QtCore.QUrl] = []
    monkeypatch.setattr(QtGui.QDesktopServices, "openUrl", lambda u: calls.append(QtCore.QUrl(u)) or True)
    return calls


def _view_with_html(app, html: str) -> DetailView:
    view = DetailView()
    view.resize(420, 300)
    view._reply_browser.setHtml(html)
    app.processEvents()
    return view


def test_link_click_never_navigates_browser(app, opened, tmp_path):
    html = "<p>conversation body MARKER</p>"
    view = _view_with_html(app, html)
    assert not view._reply_browser.openLinks()
    (tmp_path / "notes.md").write_text("FILE CONTENTS")
    view.set_repo_cwd(tmp_path)
    for target in ("notes.md", "https://example.com/x", "javascript:alert(1)", "#nowhere"):
        view._reply_browser.anchorClicked.emit(QtCore.QUrl(target))
        app.processEvents()
        text = view._reply_browser.toPlainText()
        assert "MARKER" in text and "FILE CONTENTS" not in text
        assert view._reply_browser.source().isEmpty()


def test_link_routing(app, opened, tmp_path):
    view = _view_with_html(app, "<p>x</p>")
    click = lambda s: view._on_anchor_clicked(QtCore.QUrl(s))  # noqa: E731

    click("https://example.com/a")
    click("mailto:a@b.c")
    assert [u.toString() for u in opened] == ["https://example.com/a", "mailto:a@b.c"]
    opened.clear()

    # Relative path with no repo cwd → ignored.
    click("src/foo.py")
    assert opened == []

    view.set_repo_cwd(tmp_path)
    click("src/foo.py")
    click(str(tmp_path / "abs.txt"))
    assert [Path(u.toLocalFile()) for u in opened] == [tmp_path / "src/foo.py", tmp_path / "abs.txt"]
    opened.clear()

    # Unknown schemes ignored.
    click("javascript:alert(1)")
    click("ftp://example.com/")
    assert opened == []


def test_internal_anchor_link_scrolls(app, opened, monkeypatch):
    view = _view_with_html(app, "<a name='latest-reply-top'></a><p>x</p>")
    seen = []
    monkeypatch.setattr(view._reply_browser, "scrollToAnchor", lambda a: seen.append(a))
    view._on_anchor_clicked(QtCore.QUrl("#latest-reply-top"))
    assert seen == ["latest-reply-top"] and opened == []


# ---------------------------------------------------------------- D2 labels

def test_detail_transcript_label_is_plain_text(app):
    view = DetailView()
    job = Job(id=1, transcript="<b>bold</b> <img src=x>", submitted_at=0.0, status="done")
    view.show_job(job)
    lbl = view._transcript
    assert lbl.textFormat() == QtCore.Qt.PlainText
    assert lbl.text() == "<b>bold</b> <img src=x>"
    for w in (view._status_label, view._meta_label, view._bottom_verb, view._bottom_meta):
        assert w.textFormat() == QtCore.Qt.PlainText


def test_panel_labels_are_plain_text(app):
    from holler.panel import JobRow, Toast

    job = Job(id=1, transcript="<h1>hi</h1>", submitted_at=0.0, status="done", final_text="<i>x</i>")
    row = JobRow(job)
    for w in (row._transcript, row._reply, row._ticker, row._meta):
        assert w.textFormat() == QtCore.Qt.PlainText
    assert Toast()._label.textFormat() == QtCore.Qt.PlainText


# ---------------------------------------------------------------- D3 lexer cache

def test_guessed_lexer_not_cached(monkeypatch):
    if not markdown_render._AVAILABLE:
        pytest.skip("markdown/pygments unavailable")
    markdown_render._LEXER_CACHE.clear()
    guesses = []
    real = markdown_render.guess_lexer

    def spy(code):
        guesses.append(code)
        return real(code)

    monkeypatch.setattr(markdown_render, "guess_lexer", spy)
    markdown_render.render_body("```\nimport os\nprint(os.getcwd())\n```\n")
    markdown_render.render_body("```\n<html><body>x</body></html>\n```\n")
    assert len(guesses) == 2
    assert "__guess__" not in markdown_render._LEXER_CACHE


# ---------------------------------------------------------------- D4 truncation

def test_truncated_read_count_is_approximate():
    body = "\n".join(f"line {i}" for i in range(300))
    trunc = body + "\n... [truncated]"
    assert _compact_tool_result_lines("Read", body, False) == ["Read 300 lines"]
    assert _compact_tool_result_lines("Read", trunc, False) == ["Read ~300+ lines"]
    tail = _compact_tool_result_lines("Bash", trunc, False)[-1]
    assert tail == "… 297+ more lines"
    assert _compact_tool_result_lines("Bash", "a\nb\nc\nd", False)[-1] == "… 1 more line"


# ---------------------------------------------------------------- D5 row cap

class _FakeStore(QtCore.QObject):
    job_added = QtCore.Signal(int)
    job_updated = QtCore.Signal(int)

    def __init__(self):
        super().__init__()
        self.jobs: dict[int, Job] = {}

    def get(self, jid):
        return self.jobs.get(jid)

    def all(self):
        return sorted(self.jobs.values(), key=lambda j: -j.id)

    def add(self, job):
        self.jobs[job.id] = job
        self.job_added.emit(job.id)


def test_panel_caps_finished_rows(app):
    from holler.panel import Panel

    store = _FakeStore()
    panel = Panel(store)
    cap = Panel.MAX_FINISHED_ROWS
    store.add(Job(id=1, transcript="running", submitted_at=0.0, status="running"))
    store.add(Job(id=2, transcript="queued", submitted_at=0.0, status="queued"))
    for i in range(3, 3 + cap + 20):
        store.add(Job(id=i, transcript=f"t{i}", submitted_at=0.0, status="done"))
    app.processEvents()
    ids = set(panel._rows)
    assert {1, 2} <= ids
    finished = ids - {1, 2}
    assert len(finished) == cap
    assert min(finished) == 3 + 20  # oldest finished rows dropped

    # A running job finishing still respects the cap and keeps the newest.
    store.jobs[1].status = "done"
    store.job_updated.emit(1)
    assert 1 not in panel._rows and 2 in panel._rows
    assert len(set(panel._rows) - {2}) == cap
    panel.deleteLater()
