# Composer standard battery

battery_version: 1

A fixed set of operator-voice scenarios for driving the **web composer**
end-to-end — compose *and* run — through the UI or a driver.

Each `## <case>` is one session. Turns are fired in order; the first
unlabelled fenced block under each `### turn <n>` is the operator's message,
sent byte-for-byte. A case with a `### fixture` section is driven from a file
under `fixtures/<case>/` rather than from invented data.

These prompts are transcribed from operator use, with typos corrected and
nothing else changed. Unlike `evals/composer-battery/corpus.md`, they **name
mechanisms** ("use the lookup plugin", "use a reference join") — that is
deliberate here: this battery asks whether the composer honours a mechanism
the operator asked for by name, which is a different question from whether it
picks the right one unaided.

---

## complaint_triage

Two turns. Exercises: invented CSV source, an LLM classification into a closed
vocabulary, an operator-authored reference CSV, and a `lookup` join keyed on
the LLM's own output.

### turn 1

```
I've got a handful of customer complaints - make up 6 realistic ones in a csv with an id and a complaint field, we'll be triaging them by urgency
```

### turn 2

```
great - now use an LLM to read each complaint and assign a category: billing, outage, or other (one word). I also want a lookup table with an SLA attached: create a small reference csv mapping category to response_sla_hours (billing=24, outage=4, other=48) and use the lookup plugin to attach the right response_sla_hours to each complaint by its category. Save id, complaint, category, response_sla_hours as a csv.
```

**What the case is testing**

- 6 rows in, 6 rows out.
- `category` is exactly one of `billing`, `outage`, `other`.
- `response_sla_hours` agrees with the stated mapping (billing=24, outage=4,
  other=48) for every row — i.e. the join keyed on the LLM field, and did not
  default or discard.
- The sink writes exactly `id, complaint, category, response_sla_hours`.

---

## manufacturing_leads

Three turns, each amending a committed pipeline. Exercises: incremental
authoring, an LLM rating into a closed vocabulary, and a `reference_join`
added *after* an existing LLM node.

### turn 1

```
I've got a handful of customer leads for my manufacturing business - make up 5 realistic ones in a csv with an id and 10 other fields (you can generate, just make them plausible)
```

### turn 2

```
now please have an LLM read the relevant fields and rate the quality of the lead as low, medium or high
```

### turn 3

```
after the LLM node, use a reference join to add an assignment field for jones, smith, or allens depending on if it is low, medium or high respectively.
```

**What the case is testing**

- 5 rows, 11 columns (`id` + 10) after turn 1.
- The rating is exactly one of `low`, `medium`, `high`.
- `assignment` maps low→jones, medium→smith, high→allens for every row.
- Turn 3 lands as an amendment *downstream of* the existing LLM node, not as a
  rebuild that drops turns 1–2.

---

## vendor_risk

Single turn over a supplied fixture. Exercises: an uploaded CSV source, a
verbatim operator-authored prompt with `{{ row.* }}` substitution, and
structured JSON output parsed into four fields.

### fixture

`fixtures/vendor_risk/vendors.csv` — 6 vendors, 21 columns. Field values
deliberately use semicolons, never commas, so the case does not also depend on
CSV-quoting behaviour in the run-output preview (see elspeth-7f1e148ed6).

### turn 1

The operator supplies this as the LLM node's prompt, verbatim:

```
You are a third-party risk analyst. Assess the following vendor record and respond with ONLY a JSON object.

Vendor: {{ row.vendor_name }} ({{ row.vendor_id }}), operating in {{ row.country }}, industry: {{ row.industry }}.
Contract: {{ row.contract_value_usd }} USD, {{ row.contract_start }} to {{ row.contract_end }}, payment terms {{ row.payment_terms }}, renewal status: {{ row.renewal_status }}.
Data exposure: access level {{ row.data_access_level }}, PII access: {{ row.pii_access }}.
Security posture: certification {{ row.security_certification }}, last audit {{ row.last_audit_date }} scored {{ row.audit_score }}/100 with {{ row.open_findings }} open findings ({{ row.critical_findings }} critical), {{ row.incident_count_12mo }} incidents in the last 12 months, SLA uptime {{ row.sla_uptime_pct }}%.
Account owner notes: {{ row.notes }}

Respond with exactly this format (no other text):
{"risk_tier": "low" or "medium" or "high", "risk_score": 0-100, "primary_concern": "one sentence", "recommended_action": "one sentence"}
```

**What the case is testing**

- 6 rows in, 6 rows out; every `{{ row.* }}` placeholder resolves (no literal
  braces survive into the prompt that ships).
- `risk_tier` is one of `low`, `medium`, `high`; `risk_score` is 0–100.
- `primary_concern` and `recommended_action` are present and non-empty.
- The JSON response is parsed into four separate output fields, not left as
  one opaque string.
