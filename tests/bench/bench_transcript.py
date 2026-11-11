"""Transcript-view performance benchmark for holler's DetailView.

Measures, for sessions of N committed turns (default 1 / 20 / 100 / 300) plus
one live (streaming) turn:

* ``show_ms``          initial ``DetailView.show_session`` incl. forced layout;
* ``tick_*``           per-streaming-tick cost over ~50 ticks with a growing
                       live segment: ``update_streaming(job)`` (the Sessions
                       window's path) + the throttled render the 220 ms timer
                       would fire + forced document layout. ``render`` = up to
                       layout done; ``total`` = also draining the event loop
                       (deferred scroll restore / snap);
* ``sel_survives_*``   does a text selection made in committed content survive
                       one streaming tick (while tailing / while scrolled up);
* ``scroll_*``         with the user scrolled up, does the viewport jump during
                       streaming ticks (settled jump, transient jump right
                       after setHtml, and whether the doc position at the top
                       of the viewport moved);
* ``rss_delta_mb``     peak RSS growth (``resource.getrusage``) from just
                       before ``show_session`` to the end of the run.

Each (size, run) executes in a fresh subprocess so peak RSS and Qt caches are
isolated. Data is synthetic and deterministic (fixed seed, frozen
``time.time``): markdown headings/lists/links, fenced code blocks, and tool
calls (Read/Grep/Bash/Edit) persisted through the real JobStore -> Store path
(``JobStore.apply_event``), so committed turns carry ``blocks_json`` exactly
like production.

App data is redirected to a temp dir (``holler.appdir.app_data_dir`` patched,
``holler.state`` saves no-op'd) — ~/Library/Application Support/holler is never
touched.

Not a pytest test (no ``test_`` prefix; pytest does not collect it).

Usage (from the repo root)::

    QT_QPA_PLATFORM=offscreen .venv/bin/python -m tests.bench.bench_transcript \\
        --out /path/to/outdir [--runs 3] [--sizes 1,20,100,300] [--ticks 50]

Writes ``<out>/transcript_bench_<stamp>.json`` / ``.md`` plus
``<out>/latest.json`` / ``latest.md``, and prints the table.

PHASE 4 NOTE: every reach into DetailView private state lives in the
``# ── adapter`` section below. When the live-segment rewrite lands, update
those few functions (and only those) to keep the benchmark comparable.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIZES = (1, 20, 100, 300)
NOW = 1_790_000_000.0  # frozen wall clock (matches tests/visual/scenarios.py)
VIEW_SIZE = (1000, 800)
SCROLLED_TICKS = 10
# "mixed" (default): markdown + code blocks + 0-4 tool calls per committed turn.
# "text": markdown prose/lists only (no tools, no code) - a light lower bound.
CONTENT = "mixed"


# ── adapter: the ONLY place that touches DetailView internals ───────────────
#
# Phase 4 (live-segment-only updates) kept _render_timer / _render_now: a
# streaming render now rewrites only the live turn's QTextFrame in place
# (DetailView._replace_live_segment) instead of setHtml-ing everything, so
# these adapters needed no change. Contract: "make the pending streaming
# render happen now, synchronously" and "give me the QTextBrowser".

def adapter_browser(view):
    """The transcript QTextBrowser."""
    return view._reply_browser


def adapter_flush_streaming_render(view) -> bool:
    """Run the render the throttle timer would fire, synchronously.

    ``update_streaming`` only arms a 220 ms single-shot timer; waiting for it
    would add timer slack to every sample. Stop it and invoke its slot
    directly — identical work, deterministic timing. Returns True if a render
    was pending."""
    timer = getattr(view, "_render_timer", None)
    pending = bool(timer is not None and timer.isActive()) or bool(
        getattr(view, "_pending_render", False))
    if timer is not None and timer.isActive():
        timer.stop()
    if pending:
        view._render_now()
    return pending


def adapter_live_segment_len(view) -> Optional[int]:
    """Length (chars of HTML) of the live segment, informational only."""
    fn = getattr(view, "_build_live_segment_html", None)
    try:
        return len(fn()) if fn else None
    except Exception:
        return None

# ── end adapter ──────────────────────────────────────────────────────────────


def _maxrss_bytes() -> int:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024  # Linux reports KiB


def _pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return s[k]


# ── deterministic synthetic content ─────────────────────────────────────────

_WORDS = (
    "token refresh middleware session cache layout render streaming viewport "
    "selection scroll anchor document widget signal timer queue worker retry "
    "backoff handler config schema migration index query cursor buffer frame "
    "paint event thread lock state store reply transcript capture profile"
).split()


def _sentence(rng: random.Random, n: int = 0) -> str:
    n = n or rng.randint(8, 18)
    words = [rng.choice(_WORDS) for _ in range(n)]
    if rng.random() < 0.3:
        i = rng.randrange(n)
        words[i] = f"`{words[i]}()`"
    if rng.random() < 0.25:
        i = rng.randrange(n)
        words[i] = f"**{words[i]}**"
    s = " ".join(words)
    return s[0].upper() + s[1:] + "."


def _code_block(rng: random.Random, lines: int = 0) -> str:
    lines = lines or rng.randint(6, 18)
    fn = rng.choice(_WORDS)
    body = [f"def {fn}_{rng.randint(1, 99)}(arg, *, retries=3):"]
    for i in range(lines - 2):
        w = rng.choice(_WORDS)
        body.append(f"    {w} = arg.get({w!r}, {i})  # step {i}")
    body.append(f"    return {fn!s}")
    return "```python\n" + "\n".join(body) + "\n```"


def _markdown_reply(rng: random.Random, marker: str = "") -> str:
    parts = [f"## {_sentence(rng, 5)[:-1]}"]
    if marker:
        parts.append(f"{marker} {_sentence(rng)}")
    parts.append(" ".join(_sentence(rng) for _ in range(rng.randint(2, 4))))
    parts.append("\n".join(f"- {_sentence(rng, 6)}" for _ in range(rng.randint(2, 5))))
    if rng.random() < 0.7 and CONTENT == "mixed":
        parts.append(_code_block(rng))
    parts.append(f"See [the docs](https://example.com/{rng.choice(_WORDS)}) and "
                 f"`app/{rng.choice(_WORDS)}.py:{rng.randint(1, 400)}`.")
    if rng.random() < 0.3:
        parts.append("> " + _sentence(rng))
    return "\n\n".join(parts)


def _tool_call(rng: random.Random, k: int) -> tuple[str, dict, str, bool]:
    kind = k % 4
    if kind == 0:
        path = f"/repo/app/{rng.choice(_WORDS)}.py"
        res = "\n".join(f"{i:>4}  {_sentence(rng, 6)}" for i in range(1, rng.randint(20, 80)))
        return "Read", {"file_path": path}, res, False
    if kind == 1:
        pat = rng.choice(_WORDS)
        res = "\n".join(f"app/{rng.choice(_WORDS)}.py:{rng.randint(1, 500)}:    {pat} = x"
                        for _ in range(rng.randint(3, 12)))
        return "Grep", {"pattern": pat, "path": "/repo/app"}, res, False
    if kind == 2:
        err = rng.random() < 0.15
        res = ("ERROR: exit 1\n" if err else "") + "\n".join(
            f"tests/test_{rng.choice(_WORDS)}.py ........ [{p}%]" for p in range(10, 101, 10))
        return "Bash", {"command": f"cd /repo && pytest -q -k {rng.choice(_WORDS)}"}, res, err
    path = f"/repo/app/{rng.choice(_WORDS)}.py"
    return ("Edit", {"file_path": path, "old_string": _sentence(rng, 5), "new_string": _sentence(rng, 7)},
            f"The file {path} has been updated.", False)


# ── environment ──────────────────────────────────────────────────────────────

def _hermetic_env(tmp: Path):
    """Redirect app data + freeze wall clock. Returns an ExitStack."""
    import contextlib
    from unittest import mock

    st = contextlib.ExitStack()
    import holler.appdir as appdir

    st.enter_context(mock.patch.object(appdir, "app_data_dir", lambda: tmp))
    from holler import state as hstate

    for fn in ("save", "merged_save", "merged_save_debounced", "flush_pending"):
        if hasattr(hstate, fn):
            st.enter_context(mock.patch.object(hstate, fn, lambda *a, **k: None))
    st.enter_context(mock.patch.object(time, "time", lambda: NOW))
    assert appdir.app_data_dir() == tmp
    return st


def _settle(app, ms: int = 0) -> None:
    from PySide6 import QtCore

    end = time.monotonic() + ms / 1000.0
    while True:
        QtCore.QCoreApplication.sendPostedEvents()
        app.processEvents(QtCore.QEventLoop.AllEvents, 20)
        if time.monotonic() >= end:
            break
        QtCore.QThread.msleep(5)
    app.processEvents()


def _layout(view) -> float:
    """Force a full document layout; return doc height."""
    return adapter_browser(view).document().documentLayout().documentSize().height()


# ── worker: one size, one run ────────────────────────────────────────────────

class _Feeder:
    """Drives a Job through JobStore.apply_event (the real event path)."""

    def __init__(self, jobs, job_id: int):
        from holler.sdk_session import SessionEvent

        self._ev = SessionEvent
        self.jobs = jobs
        self.job_id = job_id

    def __call__(self, kind: str, **data) -> None:
        self.jobs.apply_event(self._ev(job_id=self.job_id, kind=kind, data=data))


def _build_session(store, jobs, n_committed: int, seed: int):
    sid = store.create_session(model="claude-opus-5", transcript="benchmark session",
                               screenshot_path=None, status="idle",
                               title=f"Benchmark session ({n_committed} turns)")
    next_job = 1
    for t in range(n_committed):
        rng = random.Random(seed * 100_003 + t)
        transcript = _sentence(rng, rng.randint(6, 20))
        tid = store.add_turn(session_id=sid, transcript=transcript, screenshot_path=None, job_id=None)
        f = _Feeder(jobs, next_job)
        next_job += 1
        f("submitted", transcript=transcript, model="claude-opus-5",
          local_session_id=sid, local_turn_id=tid)
        f("started", model="claude-opus-5")
        f("text_delta", text=_sentence(rng) + "\n\n")
        for k in range(rng.randint(0, 4) if CONTENT == "mixed" else 0):
            name, inp, res, err = _tool_call(rng, t + k)
            tool_id = f"toolu_{t}_{k}"
            f("tool_use", id=tool_id, name=name, input=inp)
            f("tool_result", tool_use_id=tool_id, content=res, is_error=err)
            if rng.random() < 0.5:
                f("text_delta", text=_sentence(rng) + "\n\n")
        final = _markdown_reply(rng, marker="BENCHANCHOR0001" if t == 0 else "")
        f("text_delta", text=final)
        f("usage", message_id=f"m{t}", input_tokens=1000 + t, output_tokens=300 + t,
          cache_creation_input_tokens=0, cache_read_input_tokens=5000)
        f("result", duration_ms=1000 * rng.randint(3, 120), result=final,
          total_cost_usd=round(rng.random(), 3), is_error=False)
    # The live turn.
    live_tid = store.add_turn(session_id=sid, transcript="keep going and stream a long reply",
                              screenshot_path=None, job_id=None)
    live = _Feeder(jobs, next_job)
    live("submitted", transcript="keep going and stream a long reply", model="claude-opus-5",
         local_session_id=sid, local_turn_id=live_tid)
    live("started", model="claude-opus-5")
    live("text_delta", text="Starting on it. ")
    return sid, live


def _feed_tick(live: _Feeder, rng: random.Random, k: int) -> None:
    """One render-tick's worth of SDK events (~0.25 s of streaming)."""
    if k % 7 == 3:
        name, inp, _res, _err = _tool_call(rng, k)
        live("tool_use", id=f"live_{k}", name=name, input=inp)
        return
    if k % 7 == 4:
        _name, _inp, res, err = _tool_call(rng, k - 1)
        live("tool_result", tool_use_id=f"live_{k - 1}", content=res, is_error=err)
        live("text_delta", text="\n\n")
        return
    if k % 12 == 6:
        live("text_delta", text="\n\n" + _code_block(rng, 10) + "\n\n")
        return
    for _ in range(3):
        live("text_delta", text=_sentence(rng) + " ")
    if k % 5 == 0:
        live("text_delta", text="\n\n- " + _sentence(rng, 6) + "\n- " + _sentence(rng, 6) + "\n\n")


def _selection_survives(app, view, live, rng, k: int, cursor_factory) -> tuple[bool, str]:
    """Make a selection, run one streaming tick, report survival."""
    browser = adapter_browser(view)
    cur = cursor_factory()
    if cur is None or not cur.hasSelection():
        return False, "(could not create selection)"
    browser.setTextCursor(cur)
    app.processEvents()
    want = browser.textCursor().selectedText()
    _feed_tick(live, rng, k)
    view.update_streaming(live.jobs.get(live.job_id))
    adapter_flush_streaming_render(view)
    _layout(view)
    _settle(app)
    got = browser.textCursor().selectedText()
    return bool(want) and got == want, want


def run_worker(n_committed: int, ticks: int, seed: int) -> dict[str, Any]:
    from PySide6 import QtCore, QtGui, QtWidgets

    tmp = Path(tempfile.mkdtemp(prefix="holler-bench-"))
    env = _hermetic_env(tmp)
    with env:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
        try:
            from holler import theme

            theme.apply_app_style(app)
        except Exception:
            pass
        from holler.detail import DetailView
        from holler.jobs import JobStore
        from holler.store import Store

        store = Store(tmp / "holler.db")
        jobs = JobStore(store)
        t_build = time.perf_counter()
        sid, live = _build_session(store, jobs, n_committed, seed)
        build_s = time.perf_counter() - t_build

        view = DetailView()
        view.resize(*VIEW_SIZE)
        view.show()
        _settle(app, 50)
        browser = adapter_browser(view)
        sb = browser.verticalScrollBar()

        sess = store.get_session(sid)
        turns = store.get_turns(sid)
        tcs = store.get_tool_calls_for_turns([t.id for t in turns])
        job = jobs.get(live.job_id)
        rss0 = _maxrss_bytes()

        # 1. initial show_session (incl. forced layout).
        t0 = time.perf_counter()
        view.show_session(sess, turns, tcs, job, tmp / "captures", auto_scroll=True)
        doc_h = _layout(view)
        show_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        _settle(app)
        show_settle_ms = (time.perf_counter() - t1) * 1000
        _settle(app, 700)  # let the 600 ms entry settle window close
        at_bottom_after_show = (sb.maximum() - sb.value()) <= 2

        # 2. streaming ticks (tailing).
        rng = random.Random(seed * 7_919 + n_committed)
        render_ms: list[float] = []
        total_ms: list[float] = []
        layout_ms: list[float] = []
        bottom_ok = 0
        for k in range(ticks):
            _feed_tick(live, rng, k)
            t0 = time.perf_counter()
            view.update_streaming(jobs.get(live.job_id))
            adapter_flush_streaming_render(view)
            tl = time.perf_counter()
            _layout(view)
            t1 = time.perf_counter()
            _settle(app)
            t2 = time.perf_counter()
            layout_ms.append((t1 - tl) * 1000)
            render_ms.append((t1 - t0) * 1000)
            total_ms.append((t2 - t0) * 1000)
            bottom_ok += int((sb.maximum() - sb.value()) <= 2)
        live_seg = adapter_live_segment_len(view)
        doc_chars = browser.document().characterCount()

        # 3. selection in committed content while tailing.
        def _marker_cursor():
            c = browser.document().find("BENCHANCHOR0001")
            return c if not c.isNull() else None

        sel_tail, _ = _selection_survives(app, view, live, rng, ticks, _marker_cursor)

        # 4. user scrolls up (real scrollbar actions -> actionTriggered), then
        #    selects text at the top of the viewport and streaming continues.
        scroll: dict[str, Any] = {"applicable": False}
        sel_up: Optional[bool] = None
        if sb.maximum() > sb.pageStep():
            target = int(sb.maximum() * 0.4)
            guard = 0
            while sb.value() > target and guard < 10_000:
                sb.triggerAction(QtWidgets.QAbstractSlider.SliderSingleStepSub
                                 if sb.value() - target < sb.pageStep()
                                 else QtWidgets.QAbstractSlider.SliderPageStepSub)
                guard += 1
            _settle(app, 30)

            def _top_cursor():
                c = browser.cursorForPosition(QtCore.QPoint(8, 8))
                c.movePosition(QtGui.QTextCursor.StartOfBlock)
                c.movePosition(QtGui.QTextCursor.EndOfBlock, QtGui.QTextCursor.KeepAnchor)
                if not c.hasSelection():
                    c.movePosition(QtGui.QTextCursor.NextBlock)
                    c.movePosition(QtGui.QTextCursor.EndOfBlock, QtGui.QTextCursor.KeepAnchor)
                return c

            v0 = sb.value()
            p0 = browser.cursorForPosition(QtCore.QPoint(8, 8)).position()
            sel_up, _ = _selection_survives(app, view, live, rng, ticks + 1, _top_cursor)
            # Re-baseline after the selection tick, then measure stability.
            settled_jumps, transient_jumps, anchor_moves = [], [], 0
            v_base = v0
            for j in range(SCROLLED_TICKS):
                _feed_tick(live, rng, ticks + 2 + j)
                view.update_streaming(jobs.get(live.job_id))
                adapter_flush_streaming_render(view)
                _layout(view)
                transient_jumps.append(abs(sb.value() - v_base))
                _settle(app)
                settled_jumps.append(abs(sb.value() - v_base))
                p = browser.cursorForPosition(QtCore.QPoint(8, 8)).position()
                anchor_moves += int(p != p0)
            scroll = {
                "applicable": True,
                "start_value": v0,
                "max_settled_jump_px": max(settled_jumps),
                "max_transient_jump_px": max(transient_jumps),
                "top_anchor_moved_ticks": anchor_moves,
                "ticks": SCROLLED_TICKS,
                "stable": max(settled_jumps) <= 2 and anchor_moves == 0,
            }

        rss1 = _maxrss_bytes()
        view.close()
        view.deleteLater()
        _settle(app)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)

    return {
        "turns": n_committed,
        "ticks": ticks,
        "build_data_s": round(build_s, 3),
        "show_ms": show_ms,
        "show_settle_ms": show_settle_ms,
        "doc_height_px_at_show": doc_h,
        "at_bottom_after_show": at_bottom_after_show,
        "tick_render_ms": render_ms,
        "tick_total_ms": total_ms,
        "tick_render_median_ms": statistics.median(render_ms),
        "tick_render_p95_ms": _pct(render_ms, 95),
        "tick_total_median_ms": statistics.median(total_ms),
        "tick_total_p95_ms": _pct(total_ms, 95),
        "tick_layout_ms": layout_ms,
        "tick_layout_median_ms": statistics.median(layout_ms),
        "tick_render_first5_mean_ms": statistics.fmean(render_ms[:5]),
        "tick_render_last5_mean_ms": statistics.fmean(render_ms[-5:]),
        "tailing_ticks_at_bottom": bottom_ok,
        "live_segment_html_chars_end": live_seg,
        "doc_chars_end": doc_chars,
        "sel_survives_tailing": sel_tail,
        "sel_survives_scrolled_up": sel_up,
        "scroll": scroll,
        "rss_peak_before_mb": rss0 / 2**20,
        "rss_peak_after_mb": rss1 / 2**20,
        "rss_delta_mb": (rss1 - rss0) / 2**20,
    }


# ── orchestrator ─────────────────────────────────────────────────────────────

_AGG_KEYS = (
    "show_ms", "tick_render_median_ms", "tick_render_p95_ms", "tick_layout_median_ms",
    "tick_total_median_ms", "tick_total_p95_ms", "rss_delta_mb",
)


def _spawn(n: int, ticks: int, seed: int, content: str) -> dict[str, Any]:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "tests.bench.bench_transcript", "--worker",
           "--turns", str(n), "--ticks", str(ticks), "--seed", str(seed),
           "--content", content]
    p = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    for line in p.stdout.splitlines()[::-1]:
        if line.startswith("BENCH_RESULT "):
            return json.loads(line[len("BENCH_RESULT "):])
    raise RuntimeError(f"worker n={n} failed (rc={p.returncode}):\n{p.stdout[-2000:]}\n{p.stderr[-4000:]}")


def _agg(vals: list[float]) -> dict[str, float]:
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return {"mean": m, "stdev": sd, "cv_pct": (sd / m * 100) if m else 0.0,
            "min": min(vals), "max": max(vals)}


def _yn(v) -> str:
    return "n/a" if v is None else ("yes" if v else "NO")


def _table(results: dict[int, list[dict]], aggs: dict[int, dict]) -> str:
    lines = [
        "| turns | show_session ms | tick median ms | tick p95 ms | (layout part, median) | tick+drain median ms "
        "| tick+drain p95 ms | sel survives (tail / scrolled-up) | scroll stable (max settled / transient jump px) "
        "| peak RSS Δ MB |",
        "|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|---:|",
    ]

    def f(a):
        return f"{a['mean']:.1f} ± {a['stdev']:.1f}"

    for n, runs in results.items():
        a = aggs[n]
        tail = {r["sel_survives_tailing"] for r in runs}
        up = {r["sel_survives_scrolled_up"] for r in runs}
        sc = [r["scroll"] for r in runs]
        if all(s.get("applicable") for s in sc):
            stable = "yes" if all(s["stable"] for s in sc) else "NO"
            stable += (f" ({max(s['max_settled_jump_px'] for s in sc)} / "
                       f"{max(s['max_transient_jump_px'] for s in sc)})")
        else:
            stable = "n/a"
        lines.append(
            f"| {n} | {f(a['show_ms'])} | {f(a['tick_render_median_ms'])} | {f(a['tick_render_p95_ms'])} "
            f"| {f(a['tick_layout_median_ms'])} | {f(a['tick_total_median_ms'])} | {f(a['tick_total_p95_ms'])} "
            f"| {'/'.join(_yn(x) for x in tail)} / {'/'.join(_yn(x) for x in up)} | {stable} "
            f"| {f(a['rss_delta_mb'])} |"
        )
    return "\n".join(lines)


def _variance_table(aggs: dict[int, dict]) -> str:
    lines = ["| turns | metric | mean | stdev | CV % | min | max |", "|---:|:---|---:|---:|---:|---:|---:|"]
    for n, a in aggs.items():
        for k in _AGG_KEYS:
            x = a[k]
            lines.append(f"| {n} | {k} | {x['mean']:.2f} | {x['stdev']:.2f} | {x['cv_pct']:.1f} "
                         f"| {x['min']:.2f} | {x['max']:.2f} |")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--turns", type=int, default=1, help=argparse.SUPPRESS)
    ap.add_argument("--sizes", default=",".join(map(str, DEFAULT_SIZES)))
    ap.add_argument("--ticks", type=int, default=50)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default=os.environ.get("HOLLER_BENCH_OUT")
                    or str(Path(tempfile.gettempdir()) / "holler-bench"))
    ap.add_argument("--content", choices=("mixed", "text"), default="mixed",
                    help="committed-turn content mix (default: mixed)")
    ap.add_argument("--label", default="", help="free-form tag stored in the JSON (e.g. 'baseline')")
    args = ap.parse_args(argv)

    global CONTENT
    CONTENT = args.content
    if args.worker:
        res = run_worker(args.turns, args.ticks, args.seed)
        print("BENCH_RESULT " + json.dumps(res))
        return 0

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    results: dict[int, list[dict]] = {n: [] for n in sizes}
    for r in range(args.runs):
        for n in sizes:
            t0 = time.perf_counter()
            res = _spawn(n, args.ticks, args.seed, args.content)
            results[n].append(res)
            print(f"run {r + 1}/{args.runs}  turns={n:>4}  show={res['show_ms']:.1f}ms  "
                  f"tick med={res['tick_render_median_ms']:.1f}ms p95={res['tick_render_p95_ms']:.1f}ms  "
                  f"({time.perf_counter() - t0:.1f}s)", flush=True)

    aggs = {n: {k: _agg([x[k] for x in runs]) for k in _AGG_KEYS} for n, runs in results.items()}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    meta = {
        "label": args.label, "content": args.content, "stamp": stamp, "runs": args.runs, "ticks": args.ticks,
        "seed": args.seed, "sizes": sizes, "view_size": VIEW_SIZE,
        "python": sys.version.split()[0], "platform": sys.platform,
        "qpa": os.environ.get("QT_QPA_PLATFORM"),
    }
    try:
        import PySide6

        meta["pyside6"] = PySide6.__version__
    except Exception:
        pass
    table = _table(results, aggs)
    var = _variance_table(aggs)
    md = (f"# DetailView transcript benchmark {args.label}\n\n"
          f"`{json.dumps(meta)}`\n\nValues are mean ± stdev over {args.runs} runs "
          f"(each run: fresh process, {args.ticks} streaming ticks).\n\n{table}\n\n"
          f"## Variance\n\n{var}\n")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "aggregate": aggs,
               "runs": {str(n): rs for n, rs in results.items()}}
    for name in (f"transcript_bench_{stamp}", "latest"):
        (out / f"{name}.json").write_text(json.dumps(payload, indent=2))
        (out / f"{name}.md").write_text(md)
    print()
    print(table)
    print()
    print(var)
    print(f"\nwrote {out / f'transcript_bench_{stamp}.json'} (+ .md, latest.*)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
