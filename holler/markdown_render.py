"""Markdown → HTML pipeline for the detail window.

Uses markdown-it-py for the markdown step, Pygments for code-block syntax
highlighting against a github-dark-style palette, then wraps the output with
inline CSS so QTextBrowser renders it cleanly. Falls back to plain markdown if
the libs aren't available — caller can also use QTextBrowser.setMarkdown().
"""

from __future__ import annotations

import html
import logging
import re
import sys
from typing import Callable, Optional

from holler import theme

log = logging.getLogger(__name__)

try:
    from markdown_it import MarkdownIt
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
    from pygments.util import ClassNotFound

    _AVAILABLE = True
    # gfm-like turns on table + strikethrough — the commonmark preset doesn't,
    # which is why GFM tables used to render as raw `| header |` lines mashed
    # onto one row. Linkify is disabled (would need linkify-it-py as a
    # separate dep, and explicit [text](url) syntax is enough for our use).
    _MD = (
        MarkdownIt("gfm-like", {"html": False, "linkify": False, "breaks": False})
        .disable("linkify")
    )
    # nowrap: bare highlighted spans — _code_block() supplies the container.
    _FORMATTER = HtmlFormatter(style="github-dark", noclasses=True, nobackground=True, nowrap=True)
    _LEXER_CACHE: dict[str, object] = {}
except Exception as e:  # pragma: no cover
    log.warning("markdown_render libs unavailable: %r", e)
    _AVAILABLE = False
    _MD = None
    _FORMATTER = None
    _LEXER_CACHE = {}


# ----- Monospace font resolution -----
#
# The code/tool-call surfaces were authored on Windows with 'Cascadia Code' /
# 'Consolas' hard-coded. Neither exists on macOS, so Qt silently fell back to
# the proportional UI font there. Resolve the platform's own fixed-pitch face
# instead (Menlo on macOS) and keep the Windows stack untouched.

_WIN_MONO_FAMILY = "Cascadia Code"
_WIN_MONO_STACK = "'Cascadia Code', 'Consolas', monospace"
_MONO_FALLBACKS = {"darwin": "Menlo"}
_MONO_FAMILY: Optional[str] = None


def mono_font_family() -> str:
    """Single fixed-pitch family name for ``QFont()``.

    Derived from ``QFontDatabase.systemFont(FixedFont)`` once a
    QGuiApplication exists (cached from then on); a per-platform fallback is
    returned before that or when the system answer isn't a real installed
    family (e.g. the offscreen platform reports a literal "monospace").
    """
    global _MONO_FAMILY
    if sys.platform == "win32":
        return _WIN_MONO_FAMILY
    if _MONO_FAMILY is not None:
        return _MONO_FAMILY
    fallback = _MONO_FALLBACKS.get(sys.platform, "monospace")
    try:
        from PySide6 import QtGui
        if QtGui.QGuiApplication.instance() is None:
            return fallback
        db = QtGui.QFontDatabase
        installed = set(db.families())
        family = db.systemFont(db.SystemFont.FixedFont).family()
        # Names starting with "." are private CoreText aliases Qt can't
        # address by name from CSS; skip them like any other missing family.
        if not family or family.startswith(".") or family not in installed:
            family = fallback
    except Exception:
        return fallback
    _MONO_FAMILY = family
    return family


def mono_font_stack(quote: str = "'") -> str:
    """``font-family:`` value for QSS / HTML. ``quote`` picks the quote
    character so the stack can sit inside a single-quoted HTML attribute.

    Windows keeps the original Cascadia → Consolas → monospace chain. Other
    platforms get just the resolved family — Qt treats CSS generics as literal
    (missing) names and pays an alias scan for them (see holler.uifont).
    """
    if sys.platform == "win32":
        return _WIN_MONO_STACK.replace("'", quote)
    return f"{quote}{mono_font_family()}{quote}"


_FG = theme.FG
_FG_DIM = theme.FG_MUTED
_FG_MUTED = theme.FG_FAINT
_BG = theme.CANVAS
_CODE_BG = theme.SURFACE_HOVER
_CODE_FG = theme.FG
_BORDER = theme.BORDER
_BORDER_SOFT = theme.BORDER_SUBTLE
_LINK = theme.ACCENT
_CODE_HEAD_BG = theme.SURFACE


_BASE_CSS_TEMPLATE = f"""
body {{ background: {_BG}; color: {_FG};
        font-size: {theme.FONT_TRANSCRIPT}pt; line-height: {theme.LINE_HEIGHT_BODY};
        padding: {theme.SPACE_XS}px {theme.SPACE_MD}px {theme.SPACE_MD}px {theme.SPACE_MD}px; margin: 0; }}
p.h1 {{ color: {_FG}; font-weight: {theme.WEIGHT_BOLD}; font-size: {theme.FONT_2XL}pt; margin: 0.9em 0 0.45em 0;
        letter-spacing: {theme.TRACKING_TIGHT}; line-height: 120%; }}
p.h2 {{ color: {_FG}; font-weight: {theme.WEIGHT_BOLD}; font-size: {theme.FONT_XL}pt; margin: 0.9em 0 0.4em 0;
        line-height: 125%; }}
p.h3 {{ color: {_FG}; font-weight: {theme.WEIGHT_SEMIBOLD}; font-size: {theme.FONT_LG}pt; margin: 0.9em 0 0.35em 0;
        line-height: 130%; }}
p.h4, p.h5, p.h6 {{ font-weight: {theme.WEIGHT_SEMIBOLD}; font-size: {theme.FONT_SM}pt; margin: 0.8em 0 0.3em 0;
                   text-transform: uppercase; letter-spacing: {theme.TRACKING_CAPS_EM}; color: {_FG_DIM}; }}
p {{ margin: 0.55em 0; }}
strong {{ color: {_FG}; font-weight: {theme.WEIGHT_SEMIBOLD}; }}
em {{ color: {_FG}; font-style: italic; }}
a {{ color: {_LINK}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
ul, ol {{ margin: 0.45em 0 0.65em 1.6em; padding: 0; }}
li {{ margin: 0.22em 0; }}
li > p {{ margin: 0.2em 0; }}
code {{ background: {_CODE_BG}; padding: 1px {theme.SPACE_XS}px; border-radius: {theme.RADIUS_SM}px;
        border: 1px solid {_BORDER_SOFT};
        font-family: __MONO__;
        font-size: {theme.FONT_SM}pt; color: {_CODE_FG}; }}
pre {{ background: {_CODE_BG}; padding: {theme.SPACE_SM}px {theme.SPACE_MD}px; border-radius: {theme.RADIUS_MD}px;
       white-space: pre-wrap; word-wrap: break-word;
       border: 1px solid {_BORDER_SOFT};
       font-family: __MONO__;
       font-size: {theme.FONT_SM}pt; color: {_CODE_FG}; line-height: {theme.LINE_HEIGHT_CODE}; margin: 0.7em 0; }}
pre.code {{ background: {_CODE_BG}; padding: 0; border: none; margin: 0; }}  /* = its cell; 'transparent' blanks the cell */
pre.out {{ background: {theme.TOOL_CARD_BG}; padding: 0; border: none; border-radius: 0;
           margin: {theme.SPACE_2XS}px 0 0 0; font-size: {theme.FONT_XS}pt; line-height: {theme.LINE_HEIGHT_TOOL}; }}  /* transcript tool output = its card */
pre code {{ background: transparent; padding: 0; border: none; border-radius: 0;
            white-space: pre-wrap; word-wrap: break-word;
            font-size: {theme.FONT_SM}pt; }}
blockquote {{ color: {_FG_DIM}; border-left: 3px solid {_LINK};
             padding: 0.2em 0 0.2em {theme.SPACE_MD}px; margin: 0.7em 0;
             background: {theme.MD_QUOTE_BG}; border-radius: 0 {theme.RADIUS_SM}px {theme.RADIUS_SM}px 0; }}
table {{ border-collapse: collapse; margin: 0.8em 0; font-size: {theme.FONT_SM}pt;
         width: 100%;
         border: 1px solid {_BORDER}; border-radius: {theme.RADIUS_MD}px; }}
th {{ background: {_CODE_BG}; color: {_FG}; font-weight: {theme.WEIGHT_SEMIBOLD};
      text-align: left; padding: {theme.SPACE_SM}px {theme.SPACE_MD}px; word-wrap: break-word;
      border-bottom: 1px solid {_BORDER}; }}
td {{ padding: {theme.SPACE_SM}px {theme.SPACE_MD}px; border-top: 1px solid {_BORDER_SOFT};
      word-wrap: break-word; vertical-align: top; }}
tr:first-child td {{ border-top: none; }}
del {{ color: {_FG_MUTED}; text-decoration: line-through; }}
/* QTextDocument doesn't inherit an inline-style font-family into child
   elements once the stylesheet sets one on body — only a stylesheet rule
   cascades. Callers tag fixed-pitch surfaces (tool calls) with class='mono'. */
.mono {{ font-family: __MONO__; }}
"""


_BASE_CSS_CACHE: dict[str, str] = {}


def _base_css() -> str:
    """Document CSS with the monospace stack resolved (lazily — the family
    can only be derived once a QGuiApplication exists)."""
    mono = mono_font_stack()
    css = _BASE_CSS_CACHE.get(mono)
    if css is None:
        css = _BASE_CSS_TEMPLATE.replace("__MONO__", mono)
        _BASE_CSS_CACHE[mono] = css
    return css


_HEADING_OPEN_RE = re.compile(r"<h([1-6])>")
_HEADING_CLOSE_RE = re.compile(r"</h[1-6]>")
_HR_RE = re.compile(r"<hr\s*/?>")
_HR_HTML = f"<hr style='background-color:{theme.BORDER}; margin: 1.1em 0;'/>"

def _code_block(inner_html: str, lang: str = "", action_html: str = "") -> str:
    """A fenced code block as one bgcolor'd table cell around a bare <pre>.
    QTextDocument paints a <pre>'s own background per line (one strip per
    block, with gaps); the cell paints a single panel behind every line.

    The panel sits in a 1px ``BORDER`` frame (an outer cell padded by 1px —
    QTextDocument draws per-cell borders between header cells otherwise).
    A header strip carries the language label (left) and ``action_html``
    (right, e.g. the transcript's Copy link) when either is given."""
    code = (
        f"<td colspan='2' bgcolor='{_CODE_BG}' style='padding: {theme.SPACE_SM}px {theme.SPACE_MD}px; border: none;'>"
        f"<pre class='code'>{inner_html.rstrip(chr(10))}</pre></td>"
    )
    head = ""
    if lang or action_html:
        cell = f"bgcolor='{_CODE_HEAD_BG}' style='padding: {theme.SPACE_XS}px {theme.SPACE_MD}px; border: none;'"
        head = (
            f"<tr><td valign='middle' {cell}><p style='margin: 0; line-height: 100%; color:{_FG_DIM}; "
            f"font-size: {theme.FONT_XS}pt;'>{html.escape(lang)}</p></td>"
            f"<td valign='middle' align='right' {cell}><p style='margin: 0; line-height: 100%; "
            f"font-size: {theme.FONT_XS}pt;'>{action_html}</p></td></tr>"
        )
    return (
        f"<table width='100%' cellspacing='0' cellpadding='0' border='0' "
        f"style='margin: 0.7em 0; border: none;'><tr>"
        # bgcolor on the cell: the stylesheet's border-collapse on <table>
        # suppresses a table-level background.
        f"<td bgcolor='{_BORDER}' style='padding: 1px; border: none;'>"
        f"<table width='100%' cellspacing='0' cellpadding='0' border='0' style='margin: 0; border: none;'>"
        f"{head}<tr>{code}</tr></table></td></tr></table>"
    )


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r'\bsrc="([^"]*)"')
_IMG_ALT_RE = re.compile(r'\balt="([^"]*)"')
# holler-icon:, holler-shot:, holler://, … — the transcript's internal schemes.
_INTERNAL_SCHEME_RE = re.compile(r"\s*holler[a-z0-9+.\-]*:", re.IGNORECASE)


def _neutralise_internal_img(m: re.Match) -> str:
    """An image whose src uses one of holler's internal schemes → its alt
    text. Those images are served by the transcript browser for holler's own
    chrome (icons, screenshot thumbnails); markdown from a message must never
    reach them (a model-written ``![](holler-icon:copy/fff/30000)`` would
    render a gigabyte-sized icon)."""
    src = _IMG_SRC_RE.search(m.group(0))
    if src is None or not _INTERNAL_SCHEME_RE.match(html.unescape(src.group(1))):
        return m.group(0)
    alt = _IMG_ALT_RE.search(m.group(0))
    return alt.group(1) if alt else ""  # already HTML-escaped by markdown-it


_PRE_CODE_RE = re.compile(
    r'<pre><code(?: class="language-([^"]+)")?>(.+?)</code></pre>',
    re.DOTALL,
)


def render_body(
    md_text: str,
    *,
    highlight_code: bool = True,
    code_action: Optional[Callable[[str], str]] = None,
) -> str:
    """Return the inner HTML body for `md_text` (no <html>/<head> wrapper).

    Suitable for embedding inside a larger document the caller is assembling.
    Falls back to a simple <pre> wrapping if markdown libs are unavailable.

    ``highlight_code`` toggles Pygments. Pygments is the dominant cost of this
    function (markdown-it itself is fast); during live streaming we re-render
    the growing buffer at ~4Hz, which makes Pygments O(N²) over output length.
    Pass False during streaming, True for the final paint.

    ``code_action(raw_code) -> html`` (optional) supplies the right-hand part
    of every fenced block's header — the transcript passes a Copy link.
    """
    if not md_text:
        return ""
    if not _AVAILABLE:
        return f"<pre style='white-space: pre-wrap;'>{html.escape(md_text)}</pre>"

    body = _MD.render(md_text)
    if "<img" in body:
        body = _IMG_TAG_RE.sub(_neutralise_internal_img, body)

    # QTextDocument ignores CSS font-size on <h1>–<h6> (it layers its own
    # heading scale on top of the document font), so headings render as
    # classed paragraphs that the stylesheet can size on the type scale.
    body = _HEADING_OPEN_RE.sub(r'<p class="h\1">', body)
    body = _HEADING_CLOSE_RE.sub("</p>", body)
    # <hr> is painted in its background colour — only an inline style
    # reaches it (stylesheet rules and CSS borders are ignored).
    body = _HR_RE.sub(_HR_HTML, body)

    # QTextBrowser will not reflow a table narrower than its content — a wide
    # table forces a horizontal scrollbar across the whole transcript. Qt honors
    # the ``width`` attribute (as a percentage of the viewport) more reliably
    # than CSS ``width`` on tables, so pin every table to the viewport and let
    # the cells wrap instead of overflowing.
    body = body.replace("<table>", '<table width="100%">')

    def _action(code: str) -> str:
        return code_action(code) if code_action is not None else ""

    if not highlight_code:
        def _plain_block(m: re.Match) -> str:
            lang = html.unescape(m.group(1) or "")
            return _code_block(m.group(2), lang, _action(html.unescape(m.group(2))))
        return _PRE_CODE_RE.sub(_plain_block, body)

    def _replace_block(match: re.Match) -> str:
        lang = html.unescape(match.group(1) or "")
        code_html = match.group(2)
        code = html.unescape(code_html)
        if lang:
            lexer = _LEXER_CACHE.get(lang)
            if lexer is None:
                try:
                    lexer = get_lexer_by_name(lang, stripall=False)
                except (ClassNotFound, Exception):
                    lexer = TextLexer()
                _LEXER_CACHE[lang] = lexer
        else:
            # Untagged block: guess per block. Never cache — a guess is
            # content-specific and would mis-highlight every later block.
            try:
                lexer = guess_lexer(code)
            except (ClassNotFound, Exception):
                lexer = TextLexer()
        return _code_block(highlight(code, lexer, _FORMATTER), lang, _action(code))

    return _PRE_CODE_RE.sub(_replace_block, body)


def render(md_text: str) -> Optional[str]:
    """Return a complete HTML document for QTextBrowser.setHtml()."""
    if not _AVAILABLE:
        return None
    body = render_body(md_text)
    return f"<html><head><style>{_base_css()}</style></head><body>{body}</body></html>"


def wrap_document(body_html: str) -> str:
    """Wrap pre-built body HTML in the standard document chrome."""
    return f"<html><head><style>{_base_css()}</style></head><body>{body_html}</body></html>"
