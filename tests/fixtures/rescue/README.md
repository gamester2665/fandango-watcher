# Agent rescue calibration (Phase 6)

Use this when tuning :func:`~fandango_watcher.agent_fallback.BrowserUseFallback.build_task_prompt`
after **real** Fandango checkout misses (Complete button timeout, selector drift, modals).

## Relationship to `dump-review`

- **`dump-review`** captures **review-page** DOM into `tests/fixtures/review_pages/` for the
  **$0.00 invariant** (`extract_review_state` / `validate_invariant`). That corpus does **not**
  automatically drive the vision agent prompt.
- **Rescue calibration** is about the **failure reason string** passed into
  :class:`~fandango_watcher.agent_fallback.RescueRequest` plus optional DOM context — usually
  copied from purchase-attempt logs or `PurchaseAttempt` fields.

## Workflow

1. Reproduce a scripted failure with `purchase.mode` set so you get a log line but no card charge
   (e.g. `notify_only`, or stop before Complete in a headed run).
2. Copy the **`failure_reason`** (and `current_url`) passed to `RescueRequest` from logs or DB.
3. Append an anonymized example to **`example_failure_reasons.json`** in this directory
   (keep PII out; trim to the substring the purchaser actually records).
4. Run **`uv run pytest tests/test_rescue_calibration.py -q`** — it asserts every listed reason
   still produces a prompt that contains the non-negotiable safety clauses (no final Complete click,
   no payment data, CAPTCHA → NEEDS_HUMAN, etc.).
5. Edit **`src/fandango_watcher/agent_fallback.py`** `build_task_prompt` only when the product
   owner agrees; keep golden tests and `test_purchaser_rescue.py` green.

## Optional: rescue-on-exception

Broader Playwright exceptions still **do not** invoke the agent by default (`invoke_only_on`
limits which scripted reasons trigger rescue). Expanding that is a separate safety review — see
`PLAN.md` Phase 6 notes.
