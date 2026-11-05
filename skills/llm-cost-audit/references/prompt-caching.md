# How prompt caching works, and why it misses

A map of the mechanism and of what differs between providers. It deliberately
gives no limits, lifetimes or prices: those change, and the provider's
documentation is the source for them. This file was written in November 2026.

## Contents

- [The one rule](#the-one-rule)
- [What a cache read needs](#what-a-cache-read-needs)
- [What differs by provider](#what-differs-by-provider)
- [What breaks the prefix](#what-breaks-the-prefix)
- [Reading the usage numbers](#reading-the-usage-numbers)
- [Reading prefix_stability.py output](#reading-prefix_stabilitypy-output)

## The one rule

A prompt cache matches on the start of the prompt. The provider can reuse work
only for a prefix that is identical to one it has processed recently. A single
changed character at position N makes everything from N onward a miss, however
much identical text follows it.

So caching is decided by the order of the prompt. Content that never changes
goes first. Content that changes per request goes last.

## What a cache read needs

All of these must hold. When cache reads are zero, one of them is false.

1. **An identical prefix.** Same tool definitions, same system prompt, same
   leading messages, rendered the same way.
2. **The same model.** Cache entries are not shared between models. Routing
   some calls of a route to a second model splits the cache.
3. **A live entry.** Entries expire. If calls on a route are further apart than
   the entry lifetime, each call finds nothing to read.
4. **A long enough prefix.** Providers cache only prompts above a minimum
   length, and the minimum can differ by model. A shorter prompt is not cached
   and no error is returned.
5. **Caching switched on**, where the provider requires the request to ask for
   it.
6. **The same account scope.** Entries are not shared between organisations,
   and some providers isolate them further. Traffic split across accounts or
   workspaces reads and writes separate entries.

## What differs by provider

Check each row against the provider's current documentation before acting on
it.

| | Anthropic Messages API | OpenAI Chat Completions and Responses APIs | Google Gemini API |
|---|---|---|---|
| How caching starts | The request asks for it with `cache_control`, on a content block or on the request | Automatic for prompts above the minimum length | Automatic (implicit) on supported models, or explicit: you create a cached-content object and reference it |
| What you control | Where the cached prefix ends, and the entry lifetime | Prompt order. See the provider's guide for optional parameters that affect cache routing and retention | Prompt order for implicit caching; content and lifetime of an explicit cache |
| Cache writes in usage | Reported as `cache_creation_input_tokens` and billed at a different rate from plain input | Not reported | Not reported in the usage object; an explicit cache is also billed for the time it is stored |
| Cache reads in usage | `cache_read_input_tokens` | `prompt_tokens_details.cached_tokens` or `input_tokens_details.cached_tokens` | `usageMetadata.cachedContentTokenCount` |
| Is the cached count inside the prompt count | No. `input_tokens` excludes it | Yes | Yes |

Where to look for current numbers: each provider's prompt caching (or context
caching) guide for minimum lengths and lifetimes, and its pricing page for the
rates of cache writes and cache reads.

For the Anthropic Messages API the documented prefix order is tool definitions,
then the system prompt, then messages. A change in the tools therefore
invalidates the cached system prompt and messages as well. Some request
settings outside the prompt text can also invalidate part of the cache; the
provider's caching guide lists them.

## What breaks the prefix

| Cause | What it looks like in `prefix_stability.py` | Fix |
|---|---|---|
| Current date or time in the system prompt | Differs in every request, "looks like a timestamp or date" | Move it to the latest user message, or reduce its precision to what the task needs (a date instead of a second) and accept one new entry per change |
| Request ID, trace ID or session ID near the top | Differs in every request, "looks like a UUID" or "a long number or ID" | Remove it from the prompt. IDs belong in request metadata or logs |
| User name, tenant or locale interpolated into the system prompt | A few variants across the requests | Move per-user facts after the shared instructions. If variants are intended, check each gets enough traffic |
| Tool definitions or schemas serialised with unstable key order | "a different JSON key order" | Serialise with a fixed key order |
| Tools added, removed or reordered per request | First difference inside `tools` | Send the same tool list in the same order. Restrict tool use another way |
| Sections included conditionally (`if flag: prompt += ...`) | A few variants, first difference in the system prompt | Put optional sections after the always-present ones |
| Retrieved documents or few-shot examples placed before fixed instructions | First difference early, long identical text after it | Put fixed instructions first, retrieved content after |
| History rewritten between turns (summarising, trimming, editing earlier messages) | Not detected by the script; visible as large cache writes on every turn | Append to history rather than rewriting it; when compaction is needed, do it at one point and expect a single miss |
| A prompt change deployed during the capture | Two variants | Expected once per deploy. Capture again after the deploy |

Moving a value changes the prompt the model sees. It is usually harmless, but
it is a behaviour change: check the route's outputs after the move.

## Reading the usage numbers

Per route, from `usage_report.py`:

| Pattern | Likely state |
|---|---|
| Cache reads make up most of the prompt; cache writes are a small fraction | Healthy |
| Cache writes on every call, cache reads zero | Caching is on but the prefix changes before the cached part ends, or calls are further apart than the entry lifetime |
| No cache writes and no cache reads, long prompts | Caching is not switched on, the prefix is unstable, or the prompt is under the minimum length |
| Cache reads present but a low share | Only part of the stable content is ahead of the first changing value, or traffic is thin enough that entries often expire |
| Cache-read share drops after a release | A prompt-assembly change introduced a varying value. Compare request bodies from before and after |

Usage numbers show that the cache missed, not where. The request bodies show
where.

## Reading prefix_stability.py output

- **shared prefix**: characters at the start of the prompt that every captured
  request on the route has in common, and that length as a share of the median
  request. A cache entry shared by all of them can cover at most this much.
- **first difference**: the field path, the character offset inside that field,
  and the section (`tools`, `system` or `conversation`). The samples show the
  text on each side of the difference, marked with `>>>`.
- **distinct values**: how many different values the captured requests hold at
  that point. As many as there are requests means a per-request value. Two or
  three means variants.
- **with that span moved out of the way**: the shared prefix that would remain
  if only the differing span were gone. It counts the common text before the
  span, the common text after it in the same field, and the following fields
  up to the next difference. The gap between this and the current shared
  prefix is what fixing that one difference gains, in characters, with a rough
  token estimate.
- **OK** means the requests are identical through the tools and the system
  prompt and first differ in the conversation, which is where they should
  differ. It does not mean the route is cached: check the usage numbers.
- **unstable-prefix** means the requests differ inside the tools or the system
  prompt. It is `high` when every request differs or when only the JSON key
  order differs, and `medium` when there are a few variants.

The script reports the first difference only. After fixing it, capture new
bodies and run it again; a second varying value may be behind the first.
