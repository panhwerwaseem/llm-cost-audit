---
name: llm-cost-audit
description: Audits an LLM application's token spend and prompt-cache hit rate from real usage logs, for any provider, with bundled scripts that total tokens per route and model, rank waste, price it from a table the user supplies, and find where request prefixes stop matching. Use when an LLM or AI API bill is too high or has grown, when asked to cut token cost or reduce spend on model calls, when prompt caching shows no cache hits or a low hit rate, when cache_read_input_tokens or cached_tokens stays at zero, when someone wants cost per route, feature or model from usage logs, or before recommending a cheaper model to save money.
license: MIT
compatibility: Requires Python 3.9 or later to run the bundled scripts. No network access and no extra packages needed.
metadata:
  author: panhwerwaseem
  version: "0.1.0"
---

# LLM cost audit

A cost audit is done when three things are true: every recommendation rests on
a number measured from the user's own usage logs, every cost figure comes from
a price table the user supplied, and every fix that was applied has been
measured again on new logs. Everything below serves those three conditions.

Paths in this file are relative to this skill's directory.

## Hard rules

1. **Measure before recommending.** Make no recommendation until
   `scripts/usage_report.py` has run on real usage logs. Reading the code shows
   what could be sent, not what was sent. If there are no logs, say so and help
   add logging first.
2. **Every recommendation carries its measured number.** Name the route, the
   calls, and the tokens from the script output that the change applies to. Do
   not write "should save about half". Do not present a ceiling as a saving:
   the uncached input total of a route is a ceiling, and the shared prefix
   measured by `scripts/prefix_stability.py` is the part caching can reach.
3. **Never state a price from memory.** Prices change and your recollection of
   them is not evidence. A cost figure may appear only when the user supplied a
   price table, and the report must name that table. The numbers in
   `assets/prices.example.json` are placeholders; never present a figure
   computed from them as real money. With no table, report in tokens.
4. **No cheaper model without the risk and an eval.** You may list a model
   change as an option only together with the route, its measured tokens, a
   plain statement that output quality can change, and the requirement to run
   an eval on that route's real traffic before switching. The user decides.
5. **Find the cause before changing a prompt.** For a cache finding, run
   `scripts/prefix_stability.py` on captured request bodies and name the field
   and character where the requests diverge. Do not move prompt text or add
   cache markers on a guess.
6. **Say when a saving changes behaviour.** Trimming instructions, examples,
   retrieved context or history, or lowering an output cap, changes what the
   model does. State the change and how its effect on quality will be checked.
7. **Fixed means measured again.** Claim a cache fix only after a new usage log
   shows cache-read tokens on that route. Report the cache-read share before
   and after, and which logs each came from.

## Workflow

### 1. Get the usage log

You need a JSONL file with one record per model call: a route name, the model,
and the provider's `usage` object unchanged. `references/log-format.md` lists
the accepted shapes and what to log if the application logs nothing yet. Ask
for a period that covers normal traffic, for example one full day. If the
records have no route field, find out which field identifies the feature and
pass it with `--route-field`.

### 2. Measure tokens

```bash
python3 scripts/usage_report.py usage.jsonl
```

Check the first line: the record count and the usage shapes detected should
match what the user expects. Then read the per-route table (calls, uncached
input, cache writes, cache reads, output, cache-read share) and the ranked
findings. Each finding gives the route, the tokens involved, and the log lines
it came from.

| Finding | Meaning | Next step |
|---|---|---|
| `cache-writes-never-read` | The route pays to write cache entries and never reads one | Step 4 |
| `uncached-long-input` | Long prompts, no cache activity at all | Step 4 |
| `duplicate-requests` | The same `request_hash` was sent more than once | Ask whether repeats are deliberate, then playbook item 4 |
| `output-heavy` | Output tokens are at least the size of the prompt | Playbook item 5; caching will not help |
| `spend-concentration` | One route holds half or more of the spend | Audit that route first |

### 3. Add cost, only from the user's prices

Ask the user for current prices per million tokens for each model in the log.
Copy `assets/prices.example.json`, replace every number, set the model names to
the exact strings in the log, and remove the `placeholder` line. Then:

```bash
python3 scripts/usage_report.py usage.jsonl --prices prices.json
```

If the user cannot supply prices, stay in tokens and say so in the report.

### 4. Find why the cache misses

For each route with a cache finding or a low cache-read share, capture 5 to 20
request bodies exactly as sent to the provider (format in
`references/log-format.md`), then:

```bash
python3 scripts/prefix_stability.py requests.jsonl --route <route>
```

The script reports the shared prefix length, the field and character of the
first difference, what the differing text looks like (a timestamp, a UUID, a
different JSON key order), and how long the shared prefix would be with that
span out of the way. It reports only the first difference: fix it and run
again. `references/prompt-caching.md` explains what must match for a cache
read and how this differs by provider.

### 5. Choose fixes in order of effort

Work down `references/savings-playbook.md`. It is ordered from changes that
cost nothing and risk nothing to changes that need an eval. For each fix you
propose, write the route, the measured tokens it addresses, the change, and the
risk. Stop and ask before any change that rule 4 or rule 6 covers.

### 6. Verify

After a fix is deployed, collect a new usage log for the same routes and run
step 2 again. Compare the route's cache-read share and tokens per call with the
first run. If the share did not move, the cause is not fixed: return to step 4.

### 7. Report

Give the user this, filled in from the script output:

```
Log:             <files>, <records> calls, <period covered>
Prices:          <price table file and who supplied it> or "none, token analysis only"
Where spend is:  <top routes with their share of cost or of tokens>
Findings:
  1. <finding id> on <route> (<model>): <measured tokens>, <calls> calls
     Cause:      <field and character where requests diverge> or "not yet measured"
     Fix:        <the change>
     Addresses:  <measured tokens; say if this is a ceiling>
     Risk:       <none, or the behaviour that can change and how it will be checked>
Verified:        <route: cache-read share before -> after, and the logs used> or "not yet measured again"
Not recommended: <options considered and set aside, with the reason>
Not done:        <missing data, routes not analysed, assumptions>
```

## Edge cases

- **No usage logs.** Do not estimate from the code. Add logging
  (`references/log-format.md`), wait for a representative period, then audit.
- **Unrecognised usage shape.** A gateway or SDK may rename fields. Convert the
  records to the neutral shape. Do not guess whether a prompt count includes
  cached tokens; check the provider's documentation.
- **Short prompts or thin traffic.** Providers cache only prompts above a
  minimum length, and entries expire. A route below the minimum, or with calls
  further apart than the cache lifetime, will not benefit. Check the provider's
  current limits before recommending caching for it.
- **Few records.** A finding from a handful of calls is weak. State the record
  count and ask for a longer log.
- **Conversation routes.** `prefix_stability.py` measures the prefix shared by
  all captured requests on a route. To test the shared tools and system prompt
  of a multi-turn route, capture the first request of several conversations.
  It does not check that each turn extends the previous turn unchanged.
- **Captured request bodies contain user data.** Keep them local, do not commit
  them, and quote only the short snippet around the difference.
- **Cost is list price times tokens.** Batch discounts, long-context price
  tiers, differently priced cache lifetimes and non-token fees are not
  modelled. Say so next to any cost figure.
