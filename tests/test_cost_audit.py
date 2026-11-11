"""Cost audit: pricing, transcript parsing, and the TTL counterfactual."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from holler import cost_audit


def _turn(**kw) -> cost_audit.Turn:
    base = dict(
        at=dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc),
        project="proj",
        session="sess",
        model="claude-opus-5-5",
        raw_input=0,
        write_5m=0,
        write_1h=0,
        cache_read=0,
        output=0,
        thinking=0,
    )
    base.update(kw)
    return cost_audit.Turn(**base)


def _write_transcript(tmp_path, project: str, session: str, records: list[dict]):
    d = tmp_path / project
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _record(ts: str, model="claude-opus-5-5", **usage) -> dict:
    full = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    full.update(usage)
    return {"timestamp": ts, "message": {"model": model, "usage": full}}


# ---------------------------------------------------------------- pricing


def test_longest_prefix_wins_over_a_shorter_model_family():
    assert cost_audit.price_for("claude-opus-5-5").input == 4.00
    assert cost_audit.price_for("claude-opus-5").input == 5.00
    # A dated snapshot resolves to its base model rather than falling through.
    assert cost_audit.price_for("claude-haiku-4-5-20251001").input == 1.00


def test_unknown_model_is_unpriced_rather_than_guessed():
    assert cost_audit.price_for("some-other-llm") is None
    assert cost_audit.cost_of(_turn(model="some-other-llm", output=1_000_000)).total == 0.0


def test_a_cache_write_costs_far_more_than_the_same_tokens_read():
    """The premise the whole report rests on: writes dominate reads."""
    written = cost_audit.cost_of(_turn(write_1h=1_000_000)).cache_write
    read = cost_audit.cost_of(_turn(cache_read=1_000_000)).cache_read
    assert written == pytest.approx(8.0)  # 2x the $4.00 input rate
    assert read == pytest.approx(0.2)
    assert written / read == pytest.approx(40.0)


def test_the_two_cache_ttls_are_priced_apart():
    assert cost_audit.cost_of(_turn(write_5m=1_000_000)).cache_write == pytest.approx(5.0)
    assert cost_audit.cost_of(_turn(write_1h=1_000_000)).cache_write == pytest.approx(8.0)


# ------------------------------------------------------------- transcripts


def test_read_turns_parses_usage_and_splits_the_cache_ttls(tmp_path):
    _write_transcript(
        tmp_path,
        "projA",
        "s1",
        [
            {
                "timestamp": "2026-10-19T12:00:00.000Z",
                "message": {
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 500,
                        "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": 90000,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 700,
                            "ephemeral_5m_input_tokens": 300,
                        },
                        "output_tokens_details": {"thinking_tokens": 120},
                    },
                },
            }
        ],
    )
    turns = list(cost_audit.read_turns(tmp_path))
    assert len(turns) == 1
    turn = turns[0]
    assert (turn.write_1h, turn.write_5m) == (700, 300)
    assert turn.cache_write == 1000
    assert turn.context == 3 + 1000 + 90000
    assert turn.thinking == 120


def test_a_record_without_the_ttl_split_is_priced_at_the_cheaper_ttl(tmp_path):
    """Older transcripts carry only the total; never overstate what was billed."""
    _write_transcript(
        tmp_path, "projA", "s1", [_record("2026-10-19T12:00:00Z", cache_creation_input_tokens=1000)]
    )
    turn = next(iter(cost_audit.read_turns(tmp_path)))
    assert (turn.write_5m, turn.write_1h) == (1000, 0)


def test_unreadable_lines_are_counted_not_silently_dropped(tmp_path):
    path = _write_transcript(tmp_path, "projA", "s1", [_record("2026-10-19T12:00:00Z", output_tokens=1)])
    path.write_text(path.read_text() + '{"usage": broken json\n', encoding="utf-8")
    stats = cost_audit.ReadStats()
    turns = list(cost_audit.read_turns(tmp_path, stats=stats))
    assert len(turns) == 1
    assert stats.unparsable_lines == 1


def test_an_unpriced_model_is_reported_by_name(tmp_path):
    _write_transcript(
        tmp_path, "projA", "s1", [_record("2026-10-19T12:00:00Z", model="claude-zeta-9", output_tokens=5)]
    )
    stats = cost_audit.ReadStats()
    list(cost_audit.read_turns(tmp_path, stats=stats))
    assert stats.turns_without_model_price == {"claude-zeta-9": 1}


def test_since_and_project_filters_apply(tmp_path):
    _write_transcript(tmp_path, "projA", "s1", [_record("2026-10-01T12:00:00Z", output_tokens=1)])
    _write_transcript(tmp_path, "projB", "s2", [_record("2026-10-19T12:00:00Z", output_tokens=1)])
    since = dt.datetime(2026, 10, 10, tzinfo=dt.timezone.utc)
    assert len(list(cost_audit.read_turns(tmp_path, since=since))) == 1
    assert len(list(cost_audit.read_turns(tmp_path, project_filter="projA"))) == 1


def test_a_missing_transcript_store_raises_instead_of_reporting_zero(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(cost_audit.read_turns(tmp_path / "nope"))


# ---------------------------------------------------------- counterfactual


def test_ttl_counterfactual_prices_cheaper_writes_when_turns_are_close_together():
    start = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)
    turns = [
        _turn(at=start, write_1h=100_000),
        _turn(at=start + dt.timedelta(seconds=30), write_1h=10_000, cache_read=100_000),
    ]
    billed, alternative = cost_audit.ttl_counterfactual(turns)
    assert alternative < billed


def test_ttl_counterfactual_charges_a_rewrite_when_the_gap_exceeds_the_ttl():
    start = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)
    close = [
        _turn(at=start, write_1h=1_000),
        _turn(at=start + dt.timedelta(seconds=30), write_1h=1_000, cache_read=500_000),
    ]
    far = [
        close[0],
        _turn(at=start + dt.timedelta(hours=1), write_1h=1_000, cache_read=500_000),
    ]
    assert cost_audit.ttl_counterfactual(far)[1] > cost_audit.ttl_counterfactual(close)[1]


# ------------------------------------------------------------------ report


def test_render_names_every_bucket_and_the_ttl_verdict():
    start = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)
    turns = [_turn(at=start, write_1h=50_000, cache_read=200_000, output=2_000)]
    text = cost_audit.render(turns, cost_audit.ReadStats(files=1), days=1)
    for expected in ("cache writes", "cache reads", "output", "prompt-cache TTL", "by project"):
        assert expected in text


def test_render_says_so_rather_than_printing_an_empty_table():
    assert "No billed turns" in cost_audit.render([], cost_audit.ReadStats(), days=3)


def test_cli_emits_json(tmp_path, monkeypatch, capsys):
    _write_transcript(
        tmp_path,
        "projA",
        "s1",
        [_record(dt.datetime.now(dt.timezone.utc).isoformat(), output_tokens=1_000)],
    )
    monkeypatch.setattr(cost_audit, "projects_root", lambda: tmp_path)
    assert cost_audit.main(["--days", "1", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests"] == 1
    assert payload["output_usd"] == pytest.approx(0.02)
