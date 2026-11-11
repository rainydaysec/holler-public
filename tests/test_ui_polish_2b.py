"""Regression tests for the phase-2b visual fixes (behavioural parts only —
pixels are covered by tests/visual)."""
from __future__ import annotations

import re

from PySide6 import QtCore, QtGui, QtWidgets

from holler import theme


def _pump(app, n=3):
    for _ in range(n):
        app.processEvents()


# ── quick switcher: elide, never scroll sideways ────────────────────────────

def test_quick_switcher_elides_long_titles_without_horizontal_scroll(app, tmp_path):
    from holler import sessions_window as sw
    from holler.store import Store

    store = Store(tmp_path / "holler.db")
    try:
        long_title = "Investigate why the canvas summary panel stops updating " * 4
        store.create_session(model="m", transcript="t", screenshot_path=None, status="idle",
                             title=long_title)
        store.create_session(model="m", transcript="t", screenshot_path=None, status="idle",
                             title="short")
        d = sw.QuickSwitcherDialog(store, [])
        d.resize(640, 480)
        d.show()
        _pump(app)
        res = d._results
        assert res.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff
        assert res.horizontalScrollBar().maximum() == 0
        labels = []
        for i in range(res.count()):
            w = res.itemWidget(res.item(i))
            labels += [lbl for lbl in w.findChildren(sw.ElidedLabel)]
        long_lbl = next(lbl for lbl in labels if lbl.fullText().strip() == long_title.strip())
        assert long_lbl.text().endswith("…")
        assert long_lbl.width() <= res.viewport().width()
        assert long_lbl.toolTip().strip() == long_title.strip()
        d.close()
    finally:
        store.close()


# ── profile picker rows fit their wrapped text ──────────────────────────────

def test_profile_picker_rows_fit_wrapped_descriptions(app):
    from holler.sessions_window import ProfilePickerDialog

    d = ProfilePickerDialog()
    d.resize(520, 560)
    d.show()
    _pump(app)
    lst = d._list
    for i in range(lst.count()):
        it = lst.item(i)
        w = lst.itemWidget(it)
        need = w.layout().totalHeightForWidth(w.width()) if w.layout().hasHeightForWidth() \
            else w.layout().sizeHint().height()
        # The row widget is never squeezed below its wrapped content…
        assert w.height() >= need, (i, w.height(), need)
        # …nor padded far beyond it (the old pre-layout size hint).
        assert it.sizeHint().height() <= need + 2 * d._ITEM_PAD_V + 2 + 1, i
    d.close()


# ── panel rows wrap instead of overflowing ──────────────────────────────────

def test_panel_row_long_error_meta_does_not_widen_the_row(app):
    from holler.jobs import Job, JobStore
    from holler.panel import Panel

    jobs = JobStore(None)
    p = Panel(jobs, corner="top-right", width=380, model_label="M")
    try:
        jobs.add(Job(id=7, transcript="deploy", submitted_at=0.0, status="error",
                     error="CLIConnectionError: Cannot write to terminated process (exit code: 143) " * 2,
                     started_at=1.0, ended_at=20.0, duration_ms=19_000))
        p.resize(380, 500)
        p.show()
        _pump(app)
        row = p._rows[7]
        assert row.minimumSizeHint().width() <= p._scroll.viewport().width()
        assert row._meta.wordWrap()
    finally:
        p.hide()
        p.deleteLater()


# ── markdown: headings on the type scale, token-coloured rules ──────────────

def test_markdown_headings_render_at_token_sizes(app):
    from holler.markdown_render import render

    doc = QtGui.QTextDocument()
    doc.setHtml(render("# One\n\n## Two\n\n### Three\n\nbody text"))
    sizes = {}
    blk = doc.begin()
    while blk.isValid():
        it = blk.begin()
        if not it.atEnd():
            sizes[blk.text()] = it.fragment().charFormat().font().pointSizeF()
        blk = blk.next()
    # <hN> would ignore CSS font-size (Qt applies its own heading scale).
    assert sizes == {"One": theme.FONT_2XL, "Two": theme.FONT_XL, "Three": theme.FONT_LG,
                     "body text": theme.FONT_TRANSCRIPT}


def test_markdown_rules_and_turn_dividers_carry_the_border_token():
    from holler import detail
    from holler.markdown_render import render_body

    body = render_body("a\n\n---\n\nb")
    assert re.search(r"<hr style='background-color:" + re.escape(theme.BORDER), body)
    assert "background-color:" + theme.BORDER in detail._TURN_DIVIDER_HTML


def test_reply_text_has_no_bullet_or_wrapper():
    # Phase 4: assistant text is plain markdown on the canvas — green ● marks a
    # successful tool call only, and no layout table wraps the reply.
    from holler import detail

    html = detail._render_assistant_text_html("hello")
    assert "●" not in html and "<table" not in html and "hello" in html


# ── one UI font ─────────────────────────────────────────────────────────────

def test_ui_font_follows_the_application_font(app):
    from holler.sessions_window import HollerWordmark
    from holler.uifont import ui_font

    assert ui_font().family() == QtGui.QGuiApplication.font().family()
    assert ui_font(13).pointSizeF() == 13
    wm = HollerWordmark()
    assert wm._font.family() == QtGui.QGuiApplication.font().family()
    assert wm._font.pointSize() == theme.FONT_WORDMARK


def test_html_bodies_do_not_pin_a_font_family():
    from holler import markdown_render

    css = markdown_render._base_css()
    body_rule = css[css.index("body {"): css.index("}", css.index("body {"))]
    assert "font-family" not in body_rule


# ── sessions window: rail keeps its 30% share after a resize ────────────────

def test_rail_fraction_tracks_the_new_window_width(app, tmp_path, monkeypatch):
    """resizeEvent used to apply the 30/70 split against the splitter's
    *previous* width (it is resized after the window's resizeEvent), leaving
    the rail at 30% of the old window — e.g. 192px in a 1220px window."""
    import time

    import holler.appdir as appdir
    from holler import state as _state
    from holler.jobs import JobStore
    from holler.sessions_window import SessionsWindow
    from holler.store import Store

    monkeypatch.setattr(appdir, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(_state, "load", lambda *a, **k: {})
    monkeypatch.setattr(_state, "merged_save_debounced", lambda *a, **kw: None)
    store = Store(tmp_path / "holler.db")
    w = SessionsWindow(store, JobStore(store), tmp_path / "captures", tmp_path)
    def settle():
        end = time.monotonic() + 0.3
        while time.monotonic() < end:
            app.processEvents()

    def check(tag):
        sp = w._splitter
        want = max(160, int(sp.width() * 0.30))
        assert abs(sp.sizes()[0] - want) <= 2, (tag, sp.width(), sp.sizes(), want)

    try:
        # Sized before the first show (how the runner restores geometry) …
        w.resize(1220, 760)
        w.show()
        settle()
        check("first show")
        # … and resized while visible.
        for width in (900, 1300):
            w.resize(width, 700)
            settle()
            check(width)
    finally:
        w.close()
        w.deleteLater()
        store.close()


# ── real-platform findings ──────────────────────────────────────────────────

def test_rail_meta_line_elides_instead_of_clipping(app):
    from holler.domain import Session
    from holler.sessions_window import ElidedLabel, SessionListItem

    s = Session(id=1, sdk_session_id=None, title="t", transcript="t", screenshot_path=None,
                model="claude-opus-5", created_at=1.0, last_activity_at=1.0, status="idle",
                pinned=False, archived=False, notes="")
    row = SessionListItem(s, {"fresh_in": 1200, "output": 100, "cache_pct": 40}, now=2.0)
    assert isinstance(row._meta, ElidedLabel)
    row.resize(150, row.sizeHint().height())
    row.show()
    _pump(app)
    assert "tokens" in row._meta.fullText()
    assert row._meta.text().endswith("…")
    assert row._meta.fontMetrics().horizontalAdvance(row._meta.text()) <= row._meta.width()
    row.close()


def test_code_block_is_one_panel(app):
    from holler.markdown_render import render_body, wrap_document

    for hl in (True, False):
        body = render_body("```python\nx = 1\ny = 2\n```", highlight_code=hl)
        assert body.count("<pre") == 1 and "<td bgcolor=" in body, hl
        assert "\n</pre>" not in body  # no trailing empty line strip
    # Rendered: the panel colour sits between the two code lines too.
    b = QtWidgets.QTextBrowser()
    b.setStyleSheet(f"QTextBrowser {{ background: {theme.CANVAS}; border: none; }}")
    b.resize(300, 160)
    b.setHtml(wrap_document(render_body("```\nx = 1\ny = 2\n```")))
    b.show()
    _pump(app)
    img = b.grab().toImage()
    col = [img.pixelColor(250, y).name() for y in range(img.height())]
    first = col.index(theme.SURFACE_HOVER)
    last = len(col) - 1 - col[::-1].index(theme.SURFACE_HOVER)
    assert all(c == theme.SURFACE_HOVER for c in col[first:last + 1])
    b.close()


def test_forms_grow_fields_to_one_edge(app, monkeypatch):
    from holler import first_run
    from holler.first_run import FirstRunDialog
    from holler.settings_dialog import SettingsDialog
    from tests.visual.scenarios import fixed_config

    # The dialog's deferred auth check must not probe the real `claude` CLI.
    monkeypatch.setattr(first_run, "find_claude_cli", lambda: None)
    for dlg in (SettingsDialog(fixed_config(), on_save=lambda c, ch: None), FirstRunDialog(fixed_config())):
        forms = dlg.findChildren(QtWidgets.QFormLayout)
        assert forms
        for f in forms:
            assert f.fieldGrowthPolicy() == QtWidgets.QFormLayout.AllNonFixedFieldsGrow
        _pump(app)  # let the deferred check run while the stub is in place
        dlg.deleteLater()
    _pump(app)


def test_routing_pill_border_is_opaque():
    # A translucent rounded QSS border double-paints at the corner joins.
    assert theme.ACCENT_PILL_BORDER.startswith("#") and len(theme.ACCENT_PILL_BORDER) == 7


def test_dashboard_opens_at_the_top_even_when_taller_than_the_view(app):
    from holler.detail import DetailView

    v = DetailView()
    v.resize(500, 300)
    v.show()
    v.clear()  # clear() leaves the view tailing, as after leaving a session
    v._tailing = True
    rows = "".join(f"<p>row {i}</p>" for i in range(200))
    v.show_dashboard(f"<html><body><p>HEADLINE</p>{rows}</body></html>")
    _pump(app, 6)
    sb = v._reply_browser.verticalScrollBar()
    assert sb.maximum() > 0
    assert sb.value() == 0
    v.close()
