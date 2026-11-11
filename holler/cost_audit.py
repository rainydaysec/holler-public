"""What a holler session actually costs, measured from Claude Code's own logs.

Run it:

    python -m holler.cost_audit                # last 7 days, every project
    python -m holler.cost_audit --days 1
    python -m holler.cost_audit --project myproject
    python -m holler.cost_audit --json

Every Claude Code turn appends one JSONL record per API response under
`~/.claude/projects/<slug>/<session>.jsonl`, and each record carries the
`usage` block the API returned. That is ground truth — not an estimate — for
how many tokens a holler turn billed and in which bucket.

Why the buckets matter more than the raw totals
-----------------------------------------------
On the current Opus models a *cache write* costs 20-40x what a *cache read*
costs (Opus 5.5: $8.00/MTok at the 1h TTL vs $0.20/MTok to read). So the
expensive thing is not re-sending a long conversation — that is cache reads,
and they are nearly free — it is how much NEW content each turn appends. A
single `ps aux` or unbounded `cat` dumped into a tool result is written to
cache once at write price and then re-read for the rest of the session.

That is why the report breaks spend into cache-write / cache-read / output
instead of a single number: the three respond to completely different fixes.

Prices are per million tokens, from the Anthropic price list. Cache writes are
billed at a multiple of the input rate: 1.25x at the 5-minute TTL, 2x at the
1-hour TTL. Claude Code picks the TTL itself (1h on a Claude subscription, 5m
on an API key) and reports which one it used in `usage.cache_creation`, so the
report prices each write at the TTL that was actually billed rather than
assuming one.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

# Cache-write multipliers over the model's input rate (Anthropic price list).
WRITE_MULTIPLIER_5M = 1.25
WRITE_MULTIPLIER_1H = 2.0


@dataclass(frozen=True)
class ModelPrice:
    """Per-million-token rates for one model."""

    input: float
    output: float
    cache_read: float

    def write_rate(self, ttl: str) -> float:
        mult = WRITE_MULTIPLIER_1H if ttl == "1h" else WRITE_MULTIPLIER_5M
        return self.input * mult


# Keys are model-id prefixes; the longest matching prefix wins, so a dated
# snapshot such as "claude-haiku-4-5-20251001" resolves to its base model.
PRICES: dict[str, ModelPrice] = {
    "claude-fable-5": ModelPrice(10.00, 50.00, 0.25),
    "claude-mythos-5": ModelPrice(10.00, 50.00, 0.25),
    "claude-opus-5-5": ModelPrice(4.00, 20.00, 0.20),
    "claude-opus-5": ModelPrice(5.00, 25.00, 0.50),
    "claude-opus-4": ModelPrice(5.00, 25.00, 0.50),
    "claude-sonnet-5-5": ModelPrice(2.00, 10.00, 0.20),
    "claude-sonnet-5": ModelPrice(2.00, 10.00, 0.20),
    "claude-sonnet-4": ModelPrice(3.00, 15.00, 0.30),
    "claude-haiku-4-5": ModelPrice(1.00, 5.00, 0.10),
}


def price_for(model: str) -> Optional[ModelPrice]:
    """Longest-prefix price lookup. None for a model we have no rate for."""
    best: Optional[str] = None
    for prefix in PRICES:
        if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return PRICES[best] if best else None


@dataclass(frozen=True)
class Turn:
    """One API response, with the usage the API reported for it."""

    at: _dt.datetime
    project: str
    session: str
    model: str
    raw_input: int
    write_5m: int
    write_1h: int
    cache_read: int
    output: int
    thinking: int

    @property
    def cache_write(self) -> int:
        return self.write_5m + self.write_1h

    @property
    def context(self) -> int:
        """Tokens the model read for this request, cached or not."""
        return self.raw_input + self.cache_write + self.cache_read


@dataclass
class Spend:
    """Dollars, split by the bucket that produced them."""

    cache_write: float = 0.0
    cache_read: float = 0.0
    output: float = 0.0
    raw_input: float = 0.0

    @property
    def total(self) -> float:
        return self.cache_write + self.cache_read + self.output + self.raw_input

    def add(self, other: Spend) -> None:
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output
        self.raw_input += other.raw_input


def cost_of(turn: Turn) -> Spend:
    """Dollars this turn billed. Zero for a model with no published rate."""
    price = price_for(turn.model)
    if price is None:
        return Spend()
    return Spend(
        cache_write=(
            turn.write_1h * price.write_rate("1h") + turn.write_5m * price.write_rate("5m")
        )
        / 1e6,
        cache_read=turn.cache_read * price.cache_read / 1e6,
        output=turn.output * price.output / 1e6,
        raw_input=turn.raw_input * price.input / 1e6,
    )


def projects_root() -> Path:
    """Where Claude Code keeps its per-project transcripts."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"


@dataclass
class ReadStats:
    """What `read_turns` could not use, so the report can say so out loud."""

    files: int = 0
    unparsable_lines: int = 0
    turns_without_model_price: dict[str, int] = field(default_factory=dict)


def _parse_timestamp(raw: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def read_turns(
    root: Path,
    since: Optional[_dt.datetime] = None,
    project_filter: str = "",
    stats: Optional[ReadStats] = None,
) -> Iterator[Turn]:
    """Yield every billed turn in `root`, newest file layout first.

    Raises FileNotFoundError when `root` does not exist — a missing transcript
    store means the caller is pointed at the wrong place, not that spend was
    zero.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"no Claude Code transcript store at {root}")
    stats = stats if stats is not None else ReadStats()
    for path in sorted(root.glob("*/*.jsonl")):
        project = path.parent.name
        if project_filter and project_filter not in project:
            continue
        stats.files += 1
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    stats.unparsable_lines += 1
                    continue
                message = record.get("message") or {}
                usage = message.get("usage") or {}
                raw_at = record.get("timestamp")
                if not usage or not isinstance(raw_at, str):
                    continue
                at = _parse_timestamp(raw_at)
                if at is None:
                    stats.unparsable_lines += 1
                    continue
                if since is not None and at < since:
                    continue
                creation = usage.get("cache_creation") or {}
                write_1h = int(creation.get("ephemeral_1h_input_tokens", 0))
                write_5m = int(creation.get("ephemeral_5m_input_tokens", 0))
                total_write = int(usage.get("cache_creation_input_tokens", 0))
                if write_1h + write_5m != total_write:
                    # Older records carry only the total. Price it at the
                    # cheaper TTL so the report never overstates spend.
                    write_5m = total_write - write_1h
                model = str(message.get("model") or "")
                if model and price_for(model) is None:
                    stats.turns_without_model_price[model] = (
                        stats.turns_without_model_price.get(model, 0) + 1
                    )
                details = usage.get("output_tokens_details") or {}
                yield Turn(
                    at=at,
                    project=project,
                    session=path.stem,
                    model=model,
                    raw_input=int(usage.get("input_tokens", 0)),
                    write_5m=max(write_5m, 0),
                    write_1h=write_1h,
                    cache_read=int(usage.get("cache_read_input_tokens", 0)),
                    output=int(usage.get("output_tokens", 0)),
                    thinking=int(details.get("thinking_tokens", 0)),
                )


@dataclass
class Group:
    """Spend plus the counters that explain it, for one slice of the data."""

    label: str
    turns: int = 0
    spend: Spend = field(default_factory=Spend)
    context: int = 0
    cache_write: int = 0
    output: int = 0

    @property
    def mean_context(self) -> float:
        return self.context / self.turns if self.turns else 0.0

    @property
    def mean_new_tokens(self) -> float:
        """Average tokens this slice appended to context per turn.

        This is the number the expensive bucket tracks: cache writes are what
        each turn *adds*, and they cost 20-40x a cache read.
        """
        return self.cache_write / self.turns if self.turns else 0.0


def group_by(turns: Iterable[Turn], key) -> dict[str, Group]:
    groups: dict[str, Group] = {}
    for turn in turns:
        label = key(turn)
        group = groups.get(label)
        if group is None:
            group = groups[label] = Group(label)
        group.turns += 1
        group.spend.add(cost_of(turn))
        group.context += turn.context
        group.cache_write += turn.cache_write
        group.output += turn.output
    return groups


def ttl_counterfactual(turns: list[Turn], ttl_seconds: int = 300) -> tuple[float, float]:
    """(billed, what the 5-minute TTL would have billed) over the same turns.

    Claude Code's 1-hour TTL doubles the price of every cache write to keep the
    prefix warm across long pauses. This replays the measured turns under a
    5-minute TTL: cheaper writes, but any request whose gap from the previous
    one in the same session exceeds the TTL re-writes the prefix it would
    otherwise have read. Whether that trade wins is a property of how the
    operator actually works, so measure it rather than guess.
    """
    billed = 0.0
    alternative = 0.0
    previous: dict[str, _dt.datetime] = {}
    for turn in sorted(turns, key=lambda t: t.at):
        price = price_for(turn.model)
        spend = cost_of(turn)
        billed += spend.total
        if price is None:
            continue
        fixed = spend.output + spend.raw_input
        last = previous.get(turn.session)
        expired = last is not None and (turn.at - last).total_seconds() > ttl_seconds
        previous[turn.session] = turn.at
        write_rate = price.write_rate("5m") / 1e6
        if expired and turn.cache_read:
            alternative += fixed + (turn.cache_write + turn.cache_read) * write_rate
        else:
            alternative += (
                fixed
                + turn.cache_write * write_rate
                + turn.cache_read * price.cache_read / 1e6
            )
    return billed, alternative


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _tokens(value: float) -> str:
    if value >= 1e6:
        return f"{value / 1e6:.2f}M"
    if value >= 1e3:
        return f"{value / 1e3:.0f}k"
    return f"{value:.0f}"


def _table(title: str, groups: dict[str, Group], limit: int, width: int = 46) -> list[str]:
    rows = sorted(groups.values(), key=lambda g: -g.spend.total)[:limit]
    lines = [
        "",
        title,
        f"{'':{width}} {'turns':>6} {'write':>8} {'read':>8} {'output':>8} {'total':>9}",
    ]
    for group in rows:
        lines.append(
            f"{group.label[-width:]:{width}} {group.turns:6d} "
            f"{_money(group.spend.cache_write):>8} {_money(group.spend.cache_read):>8} "
            f"{_money(group.spend.output):>8} {_money(group.spend.total):>9}"
        )
    return lines


def render(turns: list[Turn], stats: ReadStats, days: int) -> str:
    if not turns:
        return f"No billed turns in the last {days} day(s)."
    total = Spend()
    context = 0
    writes = 0
    reads = 0
    outputs = 0
    thinking = 0
    for turn in turns:
        total.add(cost_of(turn))
        context += turn.context
        writes += turn.cache_write
        reads += turn.cache_read
        outputs += turn.output
        thinking += turn.thinking
    turn_count = len(turns)
    lines = [
        f"holler cost audit — last {days} day(s), {turn_count} API requests "
        f"across {stats.files} transcript file(s)",
        "",
        f"  total            {_money(total.total)}   ({_money(total.total / days)}/day)",
        f"  cache writes     {_money(total.cache_write):>9}  "
        f"{100 * total.cache_write / total.total:5.1f}%   {_tokens(writes)} tokens "
        f"({_tokens(writes / turn_count)} new per request)",
        f"  cache reads      {_money(total.cache_read):>9}  "
        f"{100 * total.cache_read / total.total:5.1f}%   {_tokens(reads)} tokens "
        f"({_tokens(context / turn_count)} context per request)",
        f"  output           {_money(total.output):>9}  "
        f"{100 * total.output / total.total:5.1f}%   {_tokens(outputs)} tokens "
        f"({_tokens(thinking)} thinking)",
    ]
    if total.raw_input:
        lines.append(
            f"  uncached input   {_money(total.raw_input):>9}  "
            f"{100 * total.raw_input / total.total:5.1f}%   — a non-zero figure here "
            "means a prefix is changing between requests and defeating the cache"
        )
    lines += _table("by project", group_by(turns, lambda t: t.project), limit=10)
    lines += _table("by model", group_by(turns, lambda t: t.model or "(unknown)"), limit=10)
    lines += _table(
        "by day", group_by(turns, lambda t: t.at.astimezone().strftime("%Y-%m-%d")), limit=14
    )

    sessions = group_by(turns, lambda t: t.session)
    lines += _table("most expensive sessions", sessions, limit=8)
    lines += [
        "",
        "  A session's cost grows with turns x context, so the lever is fewer and "
        "smaller tool results,",
        "  not a cheaper model: at Opus 5.5 rates one cache write costs 40 cache reads.",
    ]

    billed, alternative = ttl_counterfactual(turns)
    delta = billed - alternative
    lines += [
        "",
        "prompt-cache TTL",
        f"  billed (as Claude Code chose)      {_money(billed)}",
        f"  counterfactual at a 5-minute TTL   {_money(alternative)}  "
        f"({'saves' if delta > 0 else 'costs'} {_money(abs(delta))}, "
        f"{abs(100 * delta / billed):.1f}%)",
        "  Force it with CLAUDE_CODE_PROMPT_CACHE_TTL=5m only if that number is "
        "worth the re-writes;",
        "  a 1-hour TTL doubles every cache write but survives long pauses between turns.",
    ]

    if stats.turns_without_model_price:
        unpriced = ", ".join(
            f"{model} x{count}" for model, count in sorted(stats.turns_without_model_price.items())
        )
        lines += ["", f"  NOT PRICED (no published rate, excluded from every total): {unpriced}"]
    if stats.unparsable_lines:
        lines += ["", f"  skipped {stats.unparsable_lines} unreadable transcript line(s)"]
    return "\n".join(lines)


def _as_json(turns: list[Turn], stats: ReadStats, days: int) -> str:
    total = Spend()
    for turn in turns:
        total.add(cost_of(turn))
    billed, alternative = ttl_counterfactual(turns)
    payload = {
        "days": days,
        "requests": len(turns),
        "files": stats.files,
        "total_usd": round(total.total, 4),
        "cache_write_usd": round(total.cache_write, 4),
        "cache_read_usd": round(total.cache_read, 4),
        "output_usd": round(total.output, 4),
        "uncached_input_usd": round(total.raw_input, 4),
        "ttl_5m_counterfactual_usd": round(alternative, 4),
        "unpriced_models": stats.turns_without_model_price,
        "unparsable_lines": stats.unparsable_lines,
        "by_project": {
            label: round(group.spend.total, 4)
            for label, group in sorted(
                group_by(turns, lambda t: t.project).items(), key=lambda kv: -kv[1].spend.total
            )
        },
    }
    return json.dumps(payload, indent=2)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m holler.cost_audit",
        description="Measure holler's Claude spend from Claude Code's own transcripts.",
    )
    parser.add_argument("--days", type=int, default=7, help="how far back to look (default 7)")
    parser.add_argument("--project", default="", help="only projects whose slug contains this")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=args.days)
    stats = ReadStats()
    turns = list(read_turns(projects_root(), since, args.project, stats))
    if args.json:
        print(_as_json(turns, stats, args.days))
    else:
        print(render(turns, stats, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
