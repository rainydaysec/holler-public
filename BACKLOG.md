# Backlog

Debt spotted but deliberately deferred (out of scope for the change that surfaced it).

## Superintendent port (2026-07-07)

- **`account_failover` feature not ported.** The upstream Superintendent commit
  range also introduced auto account-failover (`holler/account_failover.py`,
  `cfg.auto_failover`). It is a separate feature and was deliberately excluded.
  Port it on its own if wanted.

## Mic diagnostics (2026-10-02)

- **Mid-band fabrications still get through.** `Transcription.looks_fabricated`
  now drops decodes above `no_speech_prob` 0.5, which covers the near-silence
  regime that invents whole sentences. Measured fabrications in the 0.27–0.46
  band (mid-word fragments, background talkers, faint speech at rms ~0.004) are
  deliberately kept: real speech tops out at 0.11 on synthetic audio, but
  separating that band needs real-microphone data we don't have, and dropping a
  turn the operator really spoke is the worse failure. Every capture logs its
  `no_speech`/`avg_logprob`, so tighten the threshold once real logs accumulate.

## Test suite (2026-10-02)

- **`test_real_listener_is_listen_only_and_hold_tap_is_transient` is flaky.**
  Fails intermittently in a full run (`_wait(lambda: hl._hold_tap is not None)`
  times out), passes every time the file runs alone. It drives a real pynput
  listener, so it is racing against OS event delivery under suite load. Needs a
  deterministic wait or a faked listener.

## Interactive session chains (2026-07-21)

- **Silent screenshots skip the chain hold.** `_dispatch_job` bails at the
  "no transcript and no strokes" guard *before* the interactive chain-hold
  intercept, so a captured screenshot with no voice and no ink is never held in
  an armed session chain. Chaining currently assumes the operator narrates each
  capture. If purely-silent screenshots need to be chainable, let an armed chain
  bypass that early-return (without masking real mic-failure messaging).

## PTT routing / new-session chains (2026-10-02)

- **A new-session chain is stranded if a session is selected mid-chain.** Chains
  are keyed by routing target (`session_chain.chain_key`), so captures held for
  the not-yet-created session stay under the `NEW_SESSION` key. Selecting a row
  while that chain holds captures swaps the Chain bar to that session's (empty)
  chain, leaving the held ones invisible until the operator deselects. They're
  not lost, but the bar should surface them (e.g. a "N held for a new session"
  affordance) rather than going quiet.

## Cost audit (2026-10-19)

- **Historical `turns.total_cost_usd` rows are still the cumulative figure.**
  `_turn_cost_usd` makes every new turn correct, but rows written before it
  still hold the subprocess running total, so a session's historical total in
  the rail stays inflated. A one-shot migration could rewrite them to per-turn
  deltas, grouping by session and resetting whenever the value drops. Not done
  because it rewrites ledger history.

- **`input/cache_creation/cache_read` may be double-counted** (`jobs.py`, the
  `"_result"` entry). Settle it by correlating one turn's `sdk_session_id` to
  its transcript records before changing the sum — a wrong fix under-counts output.

- **`max_screenshot_long_edge` is a linear dial nobody has priced against
  quality.** 1280px costs ~1300 image tokens per push-to-talk and is re-read
  for the rest of the session; 1024 would cost ~850. Needs a side-by-side on
  real captures.

- **The engineering standard silently vanishes in an installed build.**
  `sysprompt._load_standard_layer` reads `holler/standards/engineering.md`, but
  `pyproject.toml` package-data declares only `profiles/*.md` and
  `qss_assets/*.svg`, so a wheel drops the "non-negotiable" layer and logs an
  exception. Correctness, not cost.

## Superintendent (2026-10-20)

- **`tests/test_promote.py` only passes from the main checkout.** Its three
  end-to-end cases fail in any linked worktree (`git worktree add` of `main`,
  unmodified code: promote exits 1 with empty stdout/stderr), so every worker
  running the suite from its own worktree sees three red tests that are not its
  doing. `scripts/promote.py` resolves `REPO` from `__file__`; it needs to cope
  with being run out of a linked worktree, or the test needs to pin the repo.
