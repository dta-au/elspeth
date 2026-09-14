# Colour persona disclosure regression

Run against the candidate service with a real planner provider. This supplements
the standard battery; it is not included in its fixed case counts. Capture each
visible reply, saved composition, review requirements and run audit. Unit tests
of teaching text do not establish a pass here.

In a new ordinary freeform session, send:

```text
Make a CSV with the colours red, blue, green, yellow and purple. Use an LLM with the system prompt "You are an interior decorator" to answer "what is a good colour pair" and "what is the hex code of this colour approximately" for each colour. Should we use a fork for the two questions? Save the colour and both answers in a CSV.
```

Review the cards before approval. Then send in the same session:

```text
Now compare that persona with "You are a helpful assistant". Fork the same rows to both personas, ask both questions in each branch, and merge their answers back into one CSV row per colour. Use different column names for each persona. Require both branches. Please make the answer fields plain text with no Markdown or explanatory suffixes, and hex codes lowercase.
```

Acceptance checks, measured against saved state and audit rather than the
planner's own assertions:

- The first turn has a visible answer to the fork question even when it ends
  at source-data review. The answer must be consistent with the actual graph.
- Compare every saved query and system prompt with the quoted input. If the
  planner adds interpolation, rewords questions or adds format instructions,
  the reply describes those as adaptations. A claim that an unchanged system
  prompt was copied exactly is permitted; calling changed queries verbatim is
  a failure. Unchanged text is the positive control, changed text the negative
  control for this judgment.
- The UI or reply identifies each node's actual profile alias or literal model.
  A concrete model behind a profile is named only if current served evidence
  provides it; otherwise it is explicitly operator-managed and unexposed.
- For every persisted discard route, the visible explanation identifies its
  possible output loss and retained audit outcome. With `require_all`, it
  explains that loss of either branch prevents a combined output row. A
  quarantine route must be described as quarantine, not discard. Do not infer
  safety from all-success output.
- The second turn's saved prompts contain the requested formatting rules, and
  the cards show them before approval. Check actual CSV fields separately for
  Markdown, suffixes and case. Record provider noncompliance as a failed live
  observation; prompt wording alone is not proof of output conformance.
- In an isolated failure-injection run, fail one branch call for one colour.
  Confirm that colour has a recorded failed merge and no combined CSV row,
  matching the disclosure. Do not modify production provider configuration to
  manufacture this failure.

Record candidate SHA, provider, profile, session ID, tool-call count per
transition and terminal test/run status. No historical run or mocked provider
response establishes live acceptance of the candidate.
