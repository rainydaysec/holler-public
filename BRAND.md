# Holler — brand

## Concept

**HOLLER** reads as a CB-radio call sign / megaphone — the opposite of every
cold-grey clinical AI dev tool. It's voice-first push-to-talk to an AI; the
brand should feel **warm, confident, unmistakable**.

The wordmark, the signature underline, and the amber model pill are the only
places brand color appears in the main UI. Everything below the header bar
stays neutral so the chrome doesn't compete with the operator's work.

## Wordmark

`HOLLER` in tracked uppercase (~12% letter-spacing), weight 900 (Black),
13pt, painted in **Holler Amber**. To its left, a 3-bar "shout" glyph
(ascending: 6/11/16 px) tints from soft amber → mid → full amber to give
depth without a second hue. **Bar tops are rounded (1.5 px radius) so the
silhouette reads acoustic, not digital-signal/wifi.** A 2px amber underline
runs the wordmark width as the brand signature — it repeats anywhere
Holler asserts itself (splash, About, marketing).

Implemented in code as `HollerWordmark` in `holler/sessions_window.py`.
Painted with QPainter (no font/SVG dependency) so it renders identically
on every machine.

## Header chip clustering

The header status row is grouped into three semantic clusters separated by
thin (1×14 px) vertical dividers in `_HEADER_BORDER`. Same chip style
everywhere; the dividers just give the eye somewhere to land:

| Cluster | Contents |
|---------|----------|
| runtime | `_warmth` — ready / N running / N queued |
| session | `_usage` — tokens · cache% · cost |
| budget  | `_quota` — 5h % · 7d % · resets |

The wordmark→runtime divider stays at the original full 22 px height; the
cluster dividers are deliberately shorter so they read as *grouping*, not
*hierarchy*. Each cluster's divider tracks the visibility of the chip it
precedes — hide the chip, hide the divider.

## Header pills (right side)

Two pills can appear right of the chip row:

| Pill | Style | Purpose |
|------|-------|---------|
| Continuation pill (`_routing_pill`) | **outline only** (transparent bg, soft blue border) | STATE — "next PTT → continues / starts fresh" |
| Model pill (`_model_badge`)         | **filled amber** (glow bg, soft amber border) | BRAND — active model, click to cycle |

Rule: **only one filled pill on the right.** The continuation pill is state
(`_ACCENT` blue, same family as selection/focus); the model pill is brand
(amber). Letting both fill would dilute the brand and equalize state with
identity.

The model pill is set in **tracked uppercase** (`OPUS 4.8`) at weight 700
+ 112% letter-spacing — same voice as the wordmark. The wordmark shouts;
the model pill answers in the same register.

## Palette

| Token              | Hex       | Use                                       |
|--------------------|-----------|-------------------------------------------|
| Holler Amber       | `#F5A524` | Wordmark, signature stripe, model pill    |
| Amber Soft         | `#7a4f0e` | Borders/tints adjacent to amber           |
| Amber Glow         | `#f5a524` @ 13% | Pill backgrounds                    |
| Header BG          | `#0c1117` | Header bar (warmer than the cold body)    |
| Header Border      | `#1d242b` | Divider beneath the header                |
| Body BG (existing) | `#0e1216` | Workspace                                 |
| Accent Blue (kept) | `#58a6ff` | **State only** (selection, focus rings)   |

Brand color != state color. Selection / focus stays GitHub-blue so amber
never gets diluted into a generic UI tint.

## Status pips

Chip rows in the header lead with a 7px colored dot:

- `green` — running, healthy
- `green-dim` — idle, ready
- `amber` — queued / capacity-bound (matches the brand)
- `red` — error (existing _RED token)

## Anti-patterns

- Don't use Holler Amber for non-brand state (selections, errors, normal
  buttons). It dilutes the wordmark.
- Don't rebuild the wordmark with a font — it's painted by hand for
  cross-machine consistency.
- Don't extend the amber stripe to the whole header bottom edge; the stripe
  is a *signature mark* under the wordmark, not a chrome border.

## Design tokens (`holler/theme.py`)

`holler/theme.py` is the single source of UI design values; UI modules read
tokens instead of writing literals (guarded by `tests/test_theme.py`). Legacy
module constants (`_RED`, `_FG_DIM`, `_ACCENT`, `_BG_ROW_SELECTED`,
`_BRAND_AMBER`, …) remain as aliases of these tokens. Phase 2b consolidated the
2a near-duplicates, snapped type / spacing / radii to the scales below and
raised text contrast to WCAG AA (contrast is computed in `tests/test_theme.py`).

Helpers: `rgba(token, 0..255)` → `"rgba(r,g,b,a)"` (every wash uses Qt's 0–255
alpha); `qcolor(token, alpha=None)` → `QColor`; `composite(token, alpha, over)`
and `contrast_ratio(fg, bg)` for contrast checks; `button_qss(variant, …)` for a
complete primary / secondary / neutral QPushButton rule set; `PILL_BOX` pins a
pill to `PILL_HEIGHT`. Never write 8-digit hex — Qt reads it as `#AARRGGBB`.

### App-level style

`theme.apply_app_style(app)` runs once at startup (`holler/runners/full.py`,
before the first-run dialog and before any window; the visual-test harness does
the same): requests the dark colour scheme (Qt ≥ 6.8), installs a dark
`QPalette` built from the tokens (so `QFileDialog` / `QColorDialog` /
`QMessageBox` / `QInputDialog` follow) and the app stylesheet `theme.app_qss()` —
QMenu, QToolTip, thin arrow-less scrollbars, QComboBox (+ popup + chevron),
QSpinBox steppers, QCheckBox indicator, QPushButton states incl. `#primary` /
`[variant="primary|secondary"]` and an accent focus ring, input focus,
QMessageBox / QInputDialog, QScrollArea. Window stylesheets that carry a blanket
`QWidget { background: … }` append `theme.menu_qss()` and use `button_qss()`,
because that blanket rule outranks the app sheet for their children.

### Font

One UI font: the system font (`QFontDatabase.systemFont(GeneralFont)`, which
Qt installs as the application font). QSS and HTML never set `font-family` for
UI text — they inherit it; QFonts start from `holler.uifont.ui_font(size)`.
Monospace comes from `markdown_render.mono_font_family()`. The wordmark stays
painted (see above).

### Colour

| Role | Token | Value |
|------|-------|-------|
| Surface | `HEADER` / `CANVAS` | `#0c1117` / `#0e1216` |
| | `SUNKEN` / `SURFACE` / `SURFACE_HOVER` | `#11161c` / `#161c22` / `#1c242b` (hover, code, table head) |
| | `CONTROL` / `LIVE_BG` | `#21262d` / `#0d2230` |
| | `SCRIM` | `#000000` (with alpha) |
| Border | `BORDER_SUBTLE` (= `HEADER_BORDER`) | `#1d242b` |
| | `BORDER` / `BORDER_STRONG` / `LIVE_BORDER` | `#2c343c` / `#3a434d` / `#1a3a4f` |
| Text | `FG` / `FG_MUTED` / `FG_FAINT` | `#e6edf3` / `#8b949e` / `#808a95` |
| | `FG_ON_EMPHASIS` | `#ffffff` |
| Accent (state) | `ACCENT` | `#58a6ff` |
| | `ACCENT_EMPHASIS` / `_HOVER` / `_PRESSED` | `#1a5fd0` / `#1f6feb` / `#1750b0` (primary fill; white text ≥ 4.6:1) |
| Status | `SUCCESS` / `SUCCESS_DIM` | `#3fb950` / `#2a6b32` |
| | `WARNING` / `DANGER` | `#d29922` / `#FF5C5C` |
| | `PIN` / `PIN_BG` / `PIN_BORDER` | `#f9c536` / `#2a210a` / `#4d3b15` |
| Brand | `BRAND_AMBER` / `BRAND_AMBER_SOFT` / `BRAND_AMBER_DEEP` | `#F5A524` / `#7a4f0e` / `#b5720b` |
| Washes | `SELECTION_BG` | accent-emphasis @ 64 |
| | `BRAND_AMBER_GLOW` / `BRAND_AMBER_HOVER_WASH` | amber @ 34 / 46 |
| | `CARD_PULSE_DIM/MID/BRIGHT` (`PULSE_ALPHAS`) | accent @ 20 / 32 / 44 |
| | `CARD_DONE_BG` | success @ 28 |
| | `ACCENT_HOVER_WASH` / `ACCENT_PILL_BORDER` / `MD_QUOTE_BG` | accent @ 31 / 115 / 10 |
| | `ROW_STRIPE_WASH` | white @ 10 |

Merged in 2b: `SUNKEN_ALT`→`SUNKEN`, `BORDER_STRONG_ALT`→`BORDER_STRONG`,
`HEADER_BORDER`/`MD_BORDER_SOFT`→`BORDER_SUBTLE`, `MD_BORDER`→`BORDER`,
`CODE_BG`→`SURFACE_HOVER`, `SECONDARY_BUTTON_BG`→`CONTROL`,
`FG_SOFT`/`CODE_FG`→`FG`, `FG_QUIET`→`FG_MUTED`, `FG_SUBTLE`→`FG_FAINT`,
`FG_BRIGHT`→`FG_ON_EMPHASIS`, `ACCENT_LIGHT`→`ACCENT`.

Contrast (WCAG 2.x): `FG`, `FG_MUTED` ≥ 4.5:1 on every surface, the selection
fill, the done card and the brightest pulse frame; `FG_FAINT` ≥ 4.5:1 on the
surfaces and the done card — on the selection fill or a running card, faint
row text steps up to `FG_MUTED`. State glyphs / focus borders ≥ 3:1.

### Type (pt)

| Token | pt | Use |
|-------|----|-----|
| `FONT_XS` (`FONT_CAPTION`, `FONT_MODEL_PILL`) | 9 | captions, chips, meta lines — the floor |
| `FONT_SM` (`FONT_BODY`) | 10 | UI body, inputs, buttons, code |
| `FONT_MD` (`FONT_TRANSCRIPT`) | 11 | transcript body, section heads |
| `FONT_LG` | 13 | sub-titles, markdown h3, icon glyphs |
| `FONT_XL` (`FONT_TITLE`) | 16 | dialog / page titles, markdown h2 |
| `FONT_2XL` | 20 | markdown h1 |
| `FONT_DISPLAY` | 24 | the empty-dashboard headline |
| `FONT_WORDMARK` | 13 | painted wordmark |
| `GLYPH_HERO` | 48 | empty-state emoji (not text) |

Line height (QTextDocument; percentages only — a unitless value is read as
pixels): `LINE_HEIGHT_BODY` 150%, `LINE_HEIGHT_CODE` 140%, `LINE_HEIGHT_TOOL`
135%. QTextDocument ignores CSS `font-size` on `<h1>`–`<h6>`, so markdown
headings render as `<p class="hN">`.

Weights: `WEIGHT_SEMIBOLD` 600, `WEIGHT_BOLD` 700, `WEIGHT_BOLD_KEYWORD` `bold`.
Tracking: `TRACKING_TIGHT` -0.01em, `TRACKING_CAPS_EM` 0.04em, `TRACKING_CAPS_SM`
1px, `TRACKING_CAPS_MD` 1.5px, `TRACKING_BRAND_PCT` 112 (QFont percentage).

### Spacing and shape (px)

Spacing: `SPACE_XS` 4, `SPACE_SM` 8, `SPACE_MD` 12, `SPACE_LG` 16, `SPACE_XL`
24 for every margin, padding and layout spacing; `SPACE_2XS` 2 only for
hairline gaps inside one component (title → meta).

Radii: `RADIUS_SM` 4 (menu items, code, tooltips, scrollbar handles,
checkboxes), `RADIUS_MD` 6 (buttons, inputs, cards, menus), `RADIUS_PILL` 10
(chips, pills, tags). Qt draws a radius larger than half the box as a square
corner, so pills are pinned to `PILL_HEIGHT` 20 (`PILL_BOX`) instead of using a
CSS-style 999. `RADIUS_GLYPH_BAR` 1.5 is the wordmark bar tops.
