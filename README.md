# llm-cost-audit

**An agent skill that audits an LLM application's token spend and prompt-cache hit rate from real usage logs, for any provider.**

Left alone, a coding agent asked to cut an LLM bill tends to read the code, guess where the tokens go, quote prices from memory, and suggest a cheaper model. This skill replaces that with a protocol and two scripts: no recommendation before the usage logs are measured, no cost figure without a price table you supply, no cheaper model without the quality risk and an eval, and no "fixed" claim until a new log shows the cache being read.

Works with Claude Code, and with any agent that reads the open [Agent Skills](https://agentskills.io) format (Codex, Cursor, GitHub Copilot, Gemini CLI and others).

## What it looks like

A usage log where one route pays for cache writes on every call and never reads them back. No price table was given, so no cost is printed:

```console
$ python3 scripts/usage_report.py evals/files/broken-cache/usage.jsonl
records: 48 from 1 file(s)   routes: 2   models: 2   usage shapes: anthropic x48
cost: not computed. No price table was given (--prices), so this is token analysis only.
note: duplicate-request check skipped: no record has a request_hash field.

route         calls  uncached in  cache write  cache read  output  cache-read share
support-chat     24        6,852      125,040           0   5,484              0.0%
faq-answer       24        2,748        3,180      73,140   2,076             92.5%
total            48        9,600      128,220      73,140   7,560             34.7%

model          calls  uncached in  cache write  cache read  output  cache-read share
example-large     24        6,852      125,040           0   5,484              0.0%
example-small     24        2,748        3,180      73,140   2,076             92.5%
total             48        9,600      128,220      73,140   7,560             34.7%

Findings, most severe first, then by tokens involved:
1. [high] cache-writes-never-read  route=support-chat model=example-large
   24 calls wrote 125,040 tokens to the prompt cache and read 0 back.
   involved: 125,040 cache-write tokens
   evidence: 24 record(s), first at evals/files/broken-cache/usage.jsonl lines 1, 3, 5
   next: Run prefix_stability.py on captured request bodies for this route to find what changes before the cache breakpoint. If nothing can be made stable, stop writing to the cache here.
```

The captured request bodies for that route show why. A timestamp sits 81 characters into the system prompt:

```console
$ python3 scripts/prefix_stability.py evals/files/broken-cache/requests.jsonl --route support-chat
route support-chat   model example-large   requests: 6
  shared prefix: 304 chars (about 76 tokens, rough estimate), 35% of the median request
  first difference: system[0].text, character 81 (section: system); 6 distinct value(s) in 6 requests; looks like a timestamp or date
    line 1  "[..].\nCurrent time: 2026-11-03T09:>>>14:07Z\n\nYou answer customers o[..]"
    line 2  "[..].\nCurrent time: 2026-11-03T09:>>>14:52Z\n\nYou answer customers o[..]"
    line 3  "[..].\nCurrent time: 2026-11-03T09:>>>16:31Z\n\nYou answer customers o[..]"
  with that span moved out of the way, the shared prefix would be 823 chars (about 206 tokens, rough estimate)
  [high] unstable-prefix: The system prompt differs in every request (6 of 6) at system[0].text, character 81. No request can read another request's cache entry past that point.
  next: Move the changing value after the stable content (for example into the latest user message), or remove it if the model does not need it.

Findings: 1 unstable prefix(es).
```

A healthy log, priced with the placeholder table that ships with the skill. The report labels the cost as a placeholder until real prices are filled in. The `intent-router` route has no cache reads and is not flagged, because its prompts are far below the long-input threshold:

```console
$ python3 scripts/usage_report.py evals/files/healthy/usage.jsonl --prices assets/prices.example.json
records: 76 from 1 file(s)   routes: 4   models: 2   usage shapes: neutral x76
cost: from assets/prices.example.json, per million tokens, in EXAMPLE.
PLACEHOLDER PRICES: assets/prices.example.json is marked as a placeholder. The cost figures below are not real. Replace the numbers with current prices from your provider.

route           calls  uncached in  cache write  cache read  output  cache-read share  cost (PLACEHOLDER)
summarise-call     12       22,926        2,600      28,600   4,122             52.8%               45.39
draft-reply        14        7,669        3,600      46,800   6,213             80.6%               41.52
support-chat       20        5,350        4,800      91,200   4,460             90.0%               38.07
intent-router      30        7,287            0           0     240              0.0%                0.82
total              76       43,232       11,000     166,600  15,035             75.4%              125.81

model          calls  uncached in  cache write  cache read  output  cache-read share  cost (PLACEHOLDER)
example-large     46       35,945       11,000     166,600  14,795             78.0%              124.98
example-small     30        7,287            0           0     240              0.0%                0.82
total             76       43,232       11,000     166,600  15,035             75.4%              125.81

Findings: none.
```

The commands run from the `skills/llm-cost-audit` folder. The fixture numbers are synthetic and the request bodies are shortened.

## Install

Pick one route. Installing through more than one gives you the skill twice.

**Claude Code (plugin)**

```
/plugin install llm-cost-audit --marketplace panhwerwaseem/llm-cost-audit
```

On Claude Code versions before 2.1.275, use two steps:

```
/plugin marketplace add panhwerwaseem/llm-cost-audit
/plugin install llm-cost-audit@panhwerwaseem-llm-cost-audit
```

**Any agent (skills CLI)**

```bash
npx skills add panhwerwaseem/llm-cost-audit
```

**Manual**

Copy the `skills/llm-cost-audit` folder into `.claude/skills/` (Claude Code) or `.agents/skills/` (cross-agent) in your project, or into the same folder under your home directory.

To pin a release, add the tag: `/plugin marketplace add panhwerwaseem/llm-cost-audit#v0.1.0` or `npx skills add panhwerwaseem/llm-cost-audit@v0.1.0`. Third-party plugins do not update automatically; run `claude plugin update llm-cost-audit@panhwerwaseem-llm-cost-audit` to update.

## Use

Describe the problem and the skill loads on its own:

> Our model bill doubled after the last release. Usage records are in logs/usage.jsonl. Find out where the tokens go and what to fix first.

In Claude Code you can also call it directly with `/llm-cost-audit`.

The skill needs a usage log: one JSON line per model call with a route name, the model, and the provider's usage object. If your application does not log that yet, the skill starts there.

## What is in the skill

| File | Purpose |
|---|---|
| `SKILL.md` | The protocol: seven hard rules and a seven-step workflow from usage log to verified fix |
| `scripts/usage_report.py` | Reads JSONL usage records in a neutral shape or in the Anthropic Messages, OpenAI Chat Completions, OpenAI Responses or Gemini usage shapes. Reports calls, tokens and cache-read share per route and per model, cost from a price table you supply, and ranked waste findings |
| `scripts/prefix_stability.py` | Reads captured request bodies, measures the prefix that requests on a route share, and shows the first point where they differ and what the differing text looks like |
| `assets/prices.example.json` | Price table template. The numbers are placeholders, not any provider's prices |
| `references/log-format.md` | Accepted log shapes, the price table format, how every number is computed, and what is not modelled |
| `references/prompt-caching.md` | What a cache read needs, what differs by provider, and what breaks a prefix |
| `references/savings-playbook.md` | Eight fixes ordered by effort and risk, each with the measured number to cite and how to verify it |
| `evals/` | Three fixture sets (a broken cache, mixed waste, a healthy log) with eval cases, plus trigger queries |

### How the numbers are defined

- **Prompt tokens** of a call are uncached input plus cache reads plus cache writes. Where a provider's prompt count already includes cached tokens, the script subtracts them so nothing is counted twice.
- **Cache-read share** is cache-read tokens divided by prompt tokens.
- **Cost** is each token count times the price you supplied for it, per million tokens. The scripts contain no prices.
- **Shared prefix** is counted in characters of prompt content. The token figure next to it is that count divided by four, a rough estimate and labelled as one.

### What it does not do

- It does not call any provider, fetch prices, or read your billing account.
- It does not model batch discounts, service tiers, price tiers by prompt length, cache lifetimes priced differently, or non-token fees.
- It does not judge output quality. A model change always needs your own eval.
- `prefix_stability.py` reports the first difference only, and does not check that each turn of a conversation extends the previous one unchanged.

## Requirements

- Python 3.9 or later
- No packages to install and no network access

## What it reads, writes and contacts

- **Reads:** the usage logs, request-body captures and price table you pass on the command line.
- **Writes:** nothing. Both scripts print to the terminal only.
- **Runs:** no other programs.
- **Contacts:** nothing. The scripts contain no network code; CI enforces this.
- **Permissions:** the skill pre-approves no tools. Your agent asks before running commands, as usual.

Captured request bodies contain whatever your users sent. See [SECURITY.md](SECURITY.md).

## Testing status

- The scripts have unit tests that run in CI on Python 3.9 and 3.12, together with a validator for the skill format and plugin manifests. One test runs every console example in this README and compares the output.
- The eval cases in `skills/llm-cost-audit/evals/` are in the [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) format. A with-skill versus without-skill score has not been published yet; results are welcome as an issue or pull request.

## Development

```bash
python3 tools/validate.py              # skill format, manifests, no-network check
python3 -m unittest discover -s tests  # script unit tests
```

## License

[MIT](LICENSE)
