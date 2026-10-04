# Judge Quality Corpus

`v1.jsonl` is the labelled discrimination corpus for the `cicd-judge`.
Each record is a strict JSON object that becomes one `JudgeRequest`; the
expected label scores the returned `verdict` and `should_use_decorator`
exactly.

Live validation is an explicit operator opt-in. Automatic push and pull-request
CI does not run this corpus or supply it with provider credentials. The CLI,
corpus and offline tests remain available for prompt or model changes.

The command below makes paid OpenRouter calls using the operator's
`OPENROUTER_API_KEY`. Run it only when the credential owner authorizes that
validation; there is no automatic retry or provider fallback from CI.

```bash
PYTHONPATH=elspeth-lints/src uv run python -m elspeth_lints.core.cli \
  check-judge-quality \
  --corpus config/cicd/judge-quality-corpus/v1.jsonl \
  --min-accuracy 0.90
```

Cadence:

- When live validation is authorized, run it before and after a judge prompt,
  model, or policy-context edit.
- Add labelled cases when a review finds a new judge failure mode; keep
  the corpus between 10 and 30 cases so an opted-in live run remains
  bounded.
- Re-baseline expected labels only when the underlying policy changes or
  an operator-reviewed prompt change intentionally moves the decision
  boundary. Do not lower the quality threshold as a workaround for prompt
  drift; threshold changes are policy changes and need explicit review.
