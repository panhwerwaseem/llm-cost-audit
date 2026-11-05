# Log formats and how the numbers are computed

What `scripts/usage_report.py` and `scripts/prefix_stability.py` read, how each
provider's usage object is mapped, and how every number in the report is
defined.

## Contents

- [Usage log](#usage-log)
- [The neutral shape](#the-neutral-shape)
- [Provider usage shapes](#provider-usage-shapes)
- [What to log if the application logs nothing](#what-to-log-if-the-application-logs-nothing)
- [Price table](#price-table)
- [How the numbers are computed](#how-the-numbers-are-computed)
- [Findings and thresholds](#findings-and-thresholds)
- [Captured request bodies](#captured-request-bodies)
- [What is not modelled](#what-is-not-modelled)

## Usage log

JSONL: one JSON object per line, one line per model call. Blank lines are
ignored. A line that is not valid JSON, or has no recognisable token fields,
stops the run with the file and line number, unless `--skip-invalid` is given.

| Field | Required | Meaning |
|---|---|---|
| `route` | Recommended | Name of the feature or endpoint that made the call. Another field can be chosen with `--route-field`, including a dotted path such as `metadata.feature`. Records without it are grouped as `(no route)` |
| `model` | Recommended | The model string. It must match the price table exactly. `modelVersion` is read when `model` is absent. Missing values are grouped as `(unknown)` |
| `usage` | Yes, in one of three places | The provider's usage object, unchanged. The script reads `usage`, or `usageMetadata`, or token fields at the top level of the record |
| `request_hash` | Optional | Any stable hash of the full request body. Enables the duplicate-request check |

Other fields, such as timestamps, are ignored.

## The neutral shape

Every record is mapped onto four counts. Together the first three are the whole
prompt; no token is counted twice.

| Neutral field | Meaning |
|---|---|
| `input_tokens` | Prompt tokens that were neither read from nor written to a cache |
| `cache_read_tokens` | Prompt tokens served from a cache |
| `cache_write_tokens` | Prompt tokens written to a cache by this call |
| `output_tokens` | Generated tokens, including any reasoning or thinking tokens the provider bills as output |

A record already in this shape is read as is:

```json
{"route": "support-chat", "model": "example-large", "input_tokens": 212, "cache_read_tokens": 4800, "cache_write_tokens": 0, "output_tokens": 180, "request_hash": "9f2c1a7e"}
```

## Provider usage shapes

The script picks the shape from the field names present. The first line of the
report shows which shapes it detected and how many records of each.

| Shape | Detected by | Uncached input | Cache read | Cache write | Output |
|---|---|---|---|---|---|
| Anthropic Messages | `cache_read_input_tokens` or `cache_creation_input_tokens` | `input_tokens` | `cache_read_input_tokens` | `cache_creation_input_tokens` | `output_tokens` |
| OpenAI Chat Completions | `prompt_tokens` | `prompt_tokens` minus cached | `prompt_tokens_details.cached_tokens` | none reported | `completion_tokens` |
| OpenAI Responses | `input_tokens_details` | `input_tokens` minus cached | `input_tokens_details.cached_tokens` | none reported | `output_tokens` |
| Gemini | `promptTokenCount` | `promptTokenCount` minus cached | `cachedContentTokenCount` | none reported | `candidatesTokenCount` plus `thoughtsTokenCount` |
| Neutral | `input_tokens` or `output_tokens` with none of the above | `input_tokens` | `cache_read_tokens` | `cache_write_tokens` | `output_tokens` |

The difference that matters: in the Anthropic shape `input_tokens` already
excludes cached tokens, so the three prompt fields add up to the prompt. In the
OpenAI and Gemini shapes the prompt count includes the cached tokens, so the
script subtracts them. A record where the cached count is larger than the
prompt count is rejected.

Field names and their meaning are set by the providers and can change. If the
detected shape or the totals look wrong, check the provider's API reference and
convert the records to the neutral shape yourself. Do the same for any provider
or gateway not listed here.

## What to log if the application logs nothing

At the point where the application receives a model response, append one line:

- the route or feature name,
- the model string from the response,
- the response's usage object, copied unchanged,
- optionally a hash of the request body (for example SHA-256 of the serialised
  request), to detect identical requests.

Do not log prompt or response text for this purpose. The usage report needs
only counts.

## Price table

`--prices` takes a JSON file in the format of `assets/prices.example.json`:

```json
{
  "currency": "USD",
  "unit": "per_million_tokens",
  "models": {
    "<exact model string from the log>": {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
  }
}
```

- `unit` must be `per_million_tokens`. Every price is per one million tokens.
- `input` and `output` are required for each model. `cache_read` and
  `cache_write` are required only if the log has such tokens for that model.
- Model names are matched exactly. There is no prefix or alias matching, so a
  dated model string in the log needs its own entry.
- A model in the log with no entry stops the run and lists the missing names.
- `"placeholder": true` makes the report label every cost as a placeholder.
  The shipped example file has it set, with made-up numbers. Remove the line
  once real prices are filled in.
- `currency` is printed as a label only.

The script ships no prices. Take them from the provider's current pricing page
or from the user's contract.

## How the numbers are computed

- **Prompt tokens** of a call = uncached input + cache read + cache write.
- **Cache-read share** of a route or model = cache-read tokens divided by
  prompt tokens, over all its calls. It is the share of the prompt that was
  served from cache. `n/a` when there are no prompt tokens.
- **Cost** of a route = for each model on the route, each of the four token
  counts times that model's price for it, divided by one million, summed.
- **Rows are ordered** by cost when a price table is given, otherwise by total
  tokens.
- **Findings are ordered** by severity (high, medium, info), then by the cost
  of the tokens involved when prices are given, otherwise by the token count.

## Findings and thresholds

Findings are computed per route and model, because a cache entry belongs to one
model.

| Finding | Severity | Fires when | Tokens reported |
|---|---|---|---|
| `cache-writes-never-read` | high | At least 2 calls, cache-write tokens above zero, cache-read tokens zero | Cache-write tokens |
| `uncached-long-input` | medium | At least `--min-calls` calls (default 5), no cache reads or writes, and a median prompt of at least `--min-input-tokens` (default 1000) | Uncached input tokens. This is a ceiling, not a saving |
| `duplicate-requests` | medium | At least 2 calls repeat an earlier `request_hash`, and repeats are at least 5% of the route's calls | Prompt and output tokens of the repeated calls |
| `output-heavy` | info | At least `--min-calls` calls and output tokens at least equal to prompt tokens | Output tokens |
| `spend-concentration` | info | At least 3 routes and the top route holds at least 50% of cost (or of all tokens when no prices are given) | All tokens on that route |

`--min-input-tokens` is a reporting threshold, not a provider limit. Each
provider sets its own minimum cacheable prompt length per model.

Exit code 1 means at least one finding at or above `--fail-on` (default
`medium`). Info findings do not change the exit code by default.

## Captured request bodies

`scripts/prefix_stability.py` reads JSONL with one request per line:

```json
{"route": "support-chat", "body": {"model": "example-large", "system": "...", "messages": []}}
```

`body` (or `request`) holds the request exactly as sent to the provider. A line
that is the request body itself, with an optional top-level `route`, is also
accepted. Requests are grouped by route and model. The model is read from the
body's `model` field, or from a `model` field next to `body` when the body has
none.

The script compares these fields, in this order: `tools`; then `system`,
`systemInstruction`, `system_instruction`, `instructions`; then `messages`,
`input`, `contents`. Leading messages with the role `system` or `developer`
count as system prompt. `cache_control` markers are ignored. Tools first, then
system, then messages is the order documented for the Anthropic Messages API;
for other providers it is this script's convention, not a statement about how
they render the prompt.

Lengths are counted in characters of the string and number values in those
fields. JSON keys and punctuation are not counted. The token figure is the
character count divided by `--chars-per-token` (default 4). It is a rough
estimate. For a real count use the provider's tokenizer or token-counting API.

Capture the body after all templating, at the last point before the HTTP call.
A body rebuilt later from stored parts can hide the difference you are looking
for.

## What is not modelled

- Batch or asynchronous discounts, service tiers, and price tiers that depend
  on prompt length.
- Cache entries with different lifetimes priced differently. All cache-write
  tokens use the one `cache_write` price. When Anthropic-style records report
  1-hour cache writes, the report prints a note.
- Storage charges for explicitly created caches, and per-request fees for
  hosted tools.
- Image, audio and other non-text tokens that a provider prices separately
  from text tokens.
- Gemini's `toolUsePromptTokenCount`. It is not read or added to the prompt.
- Other request fields that can affect a cache, such as the tool choice or the
  response format. `prefix_stability.py` compares only the fields listed above.
- Whether each turn of a conversation extends the previous turn unchanged.
  `prefix_stability.py` measures the prefix shared by all requests on a route.
