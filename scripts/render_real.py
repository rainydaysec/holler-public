"""Render every visual-test scenario on the REAL macOS platform (cocoa QPA).

    .venv/bin/python scripts/render_real.py OUT_DIR [--scale 1.4] [--only a,b] [--pin-fonts]
                                                [--against goldens|offscreen]

Complements tests/visual (offscreen QPA, pinned Helvetica Neue / Menlo, 1x):
here widgets are rendered by the cocoa platform plugin with the real system
font (San Francisco), real HiDPI device-pixel-ratio (screen DPR x --scale,
mirroring holler's ui_scale / QT_SCALE_FACTOR) and real CoreText metrics.

Reuses tests/visual/scenarios.py unchanged (scenario builders + VisualEnv:
seeded temp store, frozen clock, redirected app data). Real-platform
differences from VisualEnv:

* the app font is restored to the system font and VisualEnv's family
  substitutions are dropped (pass --pin-fonts to keep VisualEnv's pinning,
  which isolates rasteriser/DPR differences from font differences);
* QCursor.setPos is a no-op (on cocoa it would move the user's real pointer).

Nothing is shown on the user's display: every top-level widget gets
Qt.WA_DontShowOnScreen before it is shown (on Polish and again on the Show
event, which Qt sends before the native show), the process has no Dock icon /
menu bar (QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM + Prohibited
activation policy), and after every scenario the CoreGraphics window list is
checked (CGWindowList on-screen windows of this PID, and AppKit
NSWindow.isVisible for every window of this app). A scenario that leaks a
visible window is aborted and reported as skipped.

Isolation from a running holler: separate process, app data redirected to a
temp dir for the whole run (and per scenario by VisualEnv), pynput/keyboard
imports blocked (no global hotkey listeners can be created).

Outputs in OUT_DIR:
  <name>.png          real render at native resolution (DPR = screen x scale)
  <name>.compare.png  baseline (left, upscaled to the same logical size) | real (right)
                      baseline = tests/visual/goldens/<name>.png (default), or with
                      --against offscreen a fresh offscreen render of the CURRENT code
                      (tests/visual/render_review.py, run in a subprocess into
                      OUT_DIR/_offscreen/) — use that when the goldens are stale, so
                      the comparison isolates platform differences from code changes
  index.html          contact sheet
  report.json         per-scenario metadata (sizes, DPR, status, visibility check)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _parse(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("out_dir")
    p.add_argument("--scale", type=float, default=1.4,
                   help="QT_SCALE_FACTOR (holler's ui_scale); default 1.4")
    p.add_argument("--only", default="", help="comma-separated scenario names")
    p.add_argument("--against", choices=("goldens", "offscreen"), default="goldens",
                   help="left-hand baseline: committed goldens (default) or a fresh offscreen "
                        "render of the current code")
    p.add_argument("--pin-fonts", action="store_true",
                   help="keep VisualEnv's pinned Helvetica Neue/Menlo fonts instead of the system font")
    return p.parse_args(argv)


ARGS = _parse(sys.argv[1:])

# ── platform env: must be set before PySide6 is imported ─────────────────────
os.environ["QT_QPA_PLATFORM"] = "cocoa"
os.environ["QT_SCALE_FACTOR"] = f"{ARGS.scale:g}"
# No Dock icon / menu bar / activation: don't let Qt transform the process
# into a foreground application.
os.environ["QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM"] = "1"
os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
# Global hotkey backends must never load in this process.
for _blocked in ("pynput", "keyboard"):
    sys.modules[_blocked] = None  # type: ignore[assignment]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
if APP.platformName() != "cocoa":
    sys.exit(f"expected the cocoa platform plugin, got {APP.platformName()!r}")

try:  # belt and braces: never activate / show in Dock
    import AppKit

    AppKit.NSApplication.sharedApplication().setActivationPolicy_(
        AppKit.NSApplicationActivationPolicyProhibited)
except Exception:  # pragma: no cover - pyobjc missing
    pass

from tests.visual import compare as cmp  # noqa: E402
from tests.visual import scenarios as sc  # noqa: E402

SYSTEM_FONT = QtGui.QFont(APP.font())  # .AppleSystemUIFont — what holler uses at runtime


# ── off-screen guard ─────────────────────────────────────────────────────────

class OffscreenGuard(QtCore.QObject):
    """Mark every top-level widget WA_DontShowOnScreen before it can show."""

    def __init__(self):
        super().__init__()
        self.late: list[str] = []  # windows that reached Show without the flag

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt override
        t = ev.type()
        if t in (QtCore.QEvent.Polish, QtCore.QEvent.Show, QtCore.QEvent.ParentChange):
            try:
                if isinstance(obj, QtWidgets.QWidget) and obj.isWindow() \
                        and not obj.testAttribute(QtCore.Qt.WA_DontShowOnScreen):
                    if t == QtCore.QEvent.Show and obj.testAttribute(QtCore.Qt.WA_WState_Visible):
                        self.late.append(type(obj).__name__)
                    obj.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
            except RuntimeError:
                pass
        return False


def onscreen_windows_of_this_process() -> list[dict]:
    """CoreGraphics' view of windows this PID has on screen (the ground truth)."""
    try:
        import Quartz
    except Exception:
        return []
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    pid = os.getpid()
    out = []
    for w in info:
        if int(w.get("kCGWindowOwnerPID", -1)) != pid:
            continue
        b = w.get("kCGWindowBounds", {})
        out.append({"name": str(w.get("kCGWindowName", "")), "layer": int(w.get("kCGWindowLayer", 0)),
                    "bounds": {k: float(v) for k, v in dict(b).items()}})
    return out


def visible_nswindows() -> list[dict]:
    """AppKit's view: NSWindows of this app that are ordered in (isVisible),
    even if positioned off every display. Stricter than the CG check."""
    try:
        import AppKit
    except Exception:
        return []
    out = []
    for w in AppKit.NSApp.windows() or []:
        if w.isVisible():
            f = w.frame()
            out.append({"title": str(w.title() or ""),
                        "frame": [f.origin.x, f.origin.y, f.size.width, f.size.height]})
    return out


def qt_visibility_report() -> list[dict]:
    """Per top-level: is its native window visible, and does it intersect a screen?"""
    screens = [s.geometry() for s in QtGui.QGuiApplication.screens()]
    rows = []
    for w in APP.topLevelWidgets():
        try:
            if not w.isVisible():
                continue
            h = w.windowHandle()
            native_visible = bool(h is not None and h.isVisible())
            geo = w.frameGeometry()
            rows.append({
                "widget": type(w).__name__,
                "dont_show_on_screen": w.testAttribute(QtCore.Qt.WA_DontShowOnScreen),
                "native_visible": native_visible,
                "geometry": [geo.x(), geo.y(), geo.width(), geo.height()],
                "intersects_screen": any(s.intersects(geo) for s in screens),
            })
        except RuntimeError:
            continue
    return rows


# ── image helpers ────────────────────────────────────────────────────────────

def side_by_side(golden: QtGui.QImage | None, real: QtGui.QImage, dpr: float, title: str,
                 base_label: str = "golden") -> QtGui.QImage:
    """Golden (upscaled by dpr so logical sizes line up) left, real right."""
    if golden is not None and not golden.isNull():
        g = golden.scaled(round(golden.width() * dpr), round(golden.height() * dpr),
                          QtCore.Qt.IgnoreAspectRatio, QtCore.Qt.SmoothTransformation)
    else:
        g = QtGui.QImage(real.width(), real.height(), QtGui.QImage.Format_ARGB32)
        g.fill(QtGui.QColor("#30343b"))
    gap = max(8, round(12 * dpr))
    head = round(44 * dpr)
    W = max(g.width() + gap + real.width(), round(520 * dpr))
    H = head + max(g.height(), real.height())
    out = QtGui.QImage(W, H, QtGui.QImage.Format_ARGB32)
    out.fill(QtGui.QColor("#ff00ff"))  # magenta = area outside an image (size mismatch)
    p = QtGui.QPainter(out)
    p.fillRect(0, 0, W, head, QtGui.QColor("#111317"))
    p.fillRect(g.width(), 0, gap, H, QtGui.QColor("#111317"))
    f = QtGui.QFont(SYSTEM_FONT)
    f.setPixelSize(round(13 * dpr))
    p.setFont(f)
    p.setPen(QtGui.QColor("#e6e6e6"))
    half = head // 2
    pad = round(6 * dpr)
    p.drawText(QtCore.QRect(pad, 0, W - pad, half), QtCore.Qt.AlignVCenter, title)
    p.setPen(QtGui.QColor("#9aa4b2"))
    gl = f"{base_label} 1x ×{dpr:g}" if golden is not None else f"no {base_label}"
    p.drawText(QtCore.QRect(pad, half, g.width() - pad, half), QtCore.Qt.AlignVCenter, gl)
    p.drawText(QtCore.QRect(g.width() + gap + pad, half, W, half),
               QtCore.Qt.AlignVCenter, f"real cocoa, DPR {dpr:g}")
    p.drawImage(0, head, g)
    p.drawImage(g.width() + gap, head, real)
    p.end()
    return out


def write_index(out: Path, rows: list[dict], scale: float, against: str = "goldens") -> None:
    cards = []
    for r in rows:
        name = html.escape(r["name"])
        if r["status"] != "ok":
            cards.append(f'<section class="card bad"><h2>{name}</h2><p>{html.escape(r["status"])}</p>'
                         f'<pre>{html.escape(r.get("error", ""))}</pre></section>')
            continue
        note = html.escape(r["note"])
        cards.append(
            f'<section class="card"><h2>{name}</h2><p class="meta">{note}</p>'
            f'<a href="{name}.compare.png"><img loading="lazy" src="{name}.compare.png"></a>'
            f'<p><a href="{name}.png">real only</a></p></section>')
    (out / "index.html").write_text(f"""<!doctype html><meta charset="utf-8">
<title>holler real render x{scale:g}</title>
<style>
body{{background:#15171b;color:#ddd;font:14px -apple-system,system-ui,sans-serif;margin:0;padding:16px}}
h1{{font-size:18px}} .card{{background:#1e2127;border-radius:8px;padding:12px;margin:0 0 16px}}
.card h2{{font-size:15px;margin:0 0 4px}} .meta{{color:#9aa;margin:0 0 8px;font-size:12px}}
.card img{{max-width:100%;height:auto;display:block}} .bad{{border:1px solid #c44}} a{{color:#8bf}}
</style>
<h1>holler — real cocoa render (QT_SCALE_FACTOR {scale:g}) vs offscreen {against}</h1>
<p>Left: {against} (offscreen QPA, 1x, upscaled). Right: real platform. Magenta = size mismatch.</p>
{''.join(cards)}
""", encoding="utf-8")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    out = Path(ARGS.out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    names = [n.strip() for n in ARGS.only.split(",") if n.strip()] or sorted(sc.SCENARIOS)
    unknown = [n for n in names if n not in sc.SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {unknown}; known: {sorted(sc.SCENARIOS)}")
        return 2

    if ARGS.against == "offscreen":
        import subprocess

        base_dir = out / "_offscreen"
        env = {k: v for k, v in os.environ.items()
               if k not in ("QT_QPA_PLATFORM", "QT_SCALE_FACTOR",
                            "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM")}
        env["QT_QPA_PLATFORM"] = "offscreen"
        print(f"rendering offscreen baseline of current code -> {base_dir}")
        subprocess.run([sys.executable, str(ROOT / "tests/visual/render_review.py"), str(base_dir), *names],
                       env=env, cwd=str(ROOT), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        base_dir = cmp.GOLDENS_DIR

    guard = OffscreenGuard()
    APP.installEventFilter(guard)

    import holler.appdir as appdir

    run_tmp = Path(tempfile.mkdtemp(prefix="holler-real-render-"))
    patches = [
        # The user's pointer must not move (VisualEnv / render park it at 0,0).
        mock.patch.object(QtGui.QCursor, "setPos", staticmethod(lambda *a, **k: None)),
        # App data redirected for the whole run, not only inside VisualEnv.
        mock.patch.object(appdir, "app_data_dir", lambda: run_tmp / "appdata"),
    ]
    for pt in patches:
        pt.start()

    screens = [f"{s.name()} {s.geometry().width()}x{s.geometry().height()} dpr {s.devicePixelRatio():g}"
               for s in APP.screens()]
    print(f"cocoa, QT_SCALE_FACTOR={ARGS.scale:g}, screens: {screens}, "
          f"font: {'pinned (VisualEnv)' if ARGS.pin_fonts else SYSTEM_FONT.family() + ' (system)'}")

    rows = []
    try:
        for name in names:
            row = {"name": name}
            t0 = time.monotonic()
            guard.late.clear()
            try:
                with tempfile.TemporaryDirectory(prefix="holler-real-", dir=run_tmp) as td:
                    with sc.VisualEnv(Path(td)) as env:
                        if not ARGS.pin_fonts:
                            for fam in sc._FONT_SUBSTITUTIONS:
                                QtGui.QFont.removeSubstitutions(fam)
                            APP.setFont(SYSTEM_FONT)
                            sc._flush_font_cache()
                            sc._drain_events()
                        img = sc.render(name, env)
                        cg = onscreen_windows_of_this_process()
                        ns = visible_nswindows()
                        vis = qt_visibility_report()
                        leaked = [v for v in vis if v["native_visible"] or not v["dont_show_on_screen"]]
                        row["visibility"] = {"cg_onscreen": cg, "ns_visible": ns, "qt": vis}
                        if cg or ns or leaked or guard.late:
                            row["status"] = "skipped: a window became visible on screen"
                            row["error"] = json.dumps({"cg": cg, "ns": ns, "qt": leaked, "late": guard.late}, indent=1)
                            print(f"SKIP {name}: {row['error']}")
                            rows.append(row)
                            continue
            except Exception:
                row["status"] = "error"
                row["error"] = traceback.format_exc()
                print(f"ERROR {name}:\n{row['error']}")
                rows.append(row)
                continue

            dpr = img.devicePixelRatio()
            img.setDevicePixelRatio(1.0)
            img.save(str(out / f"{name}.png"), "PNG")
            gpath = base_dir / f"{name}.png"
            golden = QtGui.QImage(str(gpath)) if gpath.exists() else None
            side_by_side(golden, img, dpr, name, ARGS.against.rstrip("s")).save(str(out / f"{name}.compare.png"), "PNG")
            lw, lh = round(img.width() / dpr), round(img.height() / dpr)
            gsz = f"{golden.width()}x{golden.height()}" if golden is not None else "none"
            row.update(status="ok", dpr=dpr, physical=[img.width(), img.height()], logical=[lw, lh],
                       baseline=gsz, seconds=round(time.monotonic() - t0, 2),
                       note=f"logical {lw}x{lh} ({ARGS.against.rstrip("s")} {gsz}) · DPR {dpr:g} · "
                            f"physical {img.width()}x{img.height()}"
                            + ("" if gsz == f"{lw}x{lh}" else " · SIZE DIFFERS FROM BASELINE"))
            rows.append(row)
            print(f"ok   {name:28s} {row['note']}  ({row['seconds']}s, nothing on screen)")
    finally:
        for pt in reversed(patches):
            pt.stop()

    write_index(out, rows, ARGS.scale, ARGS.against)
    (out / "report.json").write_text(json.dumps(
        {"scale": ARGS.scale, "against": ARGS.against, "screens": screens, "pin_fonts": ARGS.pin_fonts, "scenarios": rows},
        indent=1, default=str), encoding="utf-8")
    bad = [r["name"] for r in rows if r["status"] != "ok"]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} rendered -> {out / 'index.html'}")
    if bad:
        print(f"not rendered: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # Skip interpreter teardown of Qt/pyobjc objects (can crash on cocoa).
    os._exit(code)
