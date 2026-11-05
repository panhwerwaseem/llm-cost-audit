# Savings playbook

Fixes ordered by effort and risk, lowest first. Work down the list and stop
when the remaining spend is not worth the next step. For every fix: cite the
measured number, make one change, then measure again.

## Contents

- [Before any fix](#before-any-fix)
- [1. Stop cache writes that are never read](#1-stop-cache-writes-that-are-never-read)
- [2. Make the prefix stable](#2-make-the-prefix-stable)
- [3. Turn caching on for a long stable prefix](#3-turn-caching-on-for-a-long-stable-prefix)
- [4. Reuse responses to identical requests](#4-reuse-responses-to-identical-requests)
- [5. Bound the output](#5-bound-the-output)
- [6. Trim the input](#6-trim-the-input)
- [7. Move work that can wait to a batch tier](#7-move-work-that-can-wait-to-a-batch-tier)
- [8. Change the model or its reasoning settings](#8-change-the-model-or-its-reasoning-settings)
- [What not to do](#what-not-to-do)

## Before any fix

- Start with the route that holds the most spend. A perfect fix on a route with
  2% of the tokens saves at most 2%.
- Items 1 to 3 do not change what the model is asked. Items 4 to 8 can change
  results; each names the check it needs.
- A number from the usage log is a measurement. A number for what a fix will
  save is an estimate until the next log confirms it. Label them differently.

## 1. Stop cache writes that are never read

**When.** `cache-writes-never-read`: a route writes cache entries on every call
and reads none.

**Cite.** The route's cache-write tokens and call count.

**Change.** First try item 2, because a stable prefix turns these writes into
reads. If the prefix cannot be made stable, or calls are further apart than the
entry lifetime, stop asking for caching on this route so the tokens are billed
as plain input.

**Risk.** None. The prompt is unchanged.

**Verify.** Cache-write tokens on the route fall to zero, or cache reads appear.

## 2. Make the prefix stable

**When.** `prefix_stability.py` reports `unstable-prefix`, or a route has long
prompts and a low cache-read share.

**Cite.** The field and character of the first difference, the number of
distinct values, and the "with that span moved out of the way" length.

**Change.** Move the varying value after the stable content, usually into the
latest user message. Remove it if the model does not need it. Fix
serialisation so tools and schemas are byte-identical between requests.
`references/prompt-caching.md` lists the common causes and the fix for each.

**Risk.** Low. The model sees the same information in a different place. Check
a sample of outputs.

**Verify.** `prefix_stability.py` on new captures shows the difference gone,
and the next usage log shows cache reads on the route.

## 3. Turn caching on for a long stable prefix

**When.** `uncached-long-input` on a provider where the request must ask for
caching, and `prefix_stability.py` shows a long shared prefix.

**Cite.** The shared prefix length and the route's calls. The saving is bounded
by the shared prefix times the calls that arrive while an entry is alive, not
by the route's whole input.

**Change.** Mark the end of the shared prefix as the cache boundary, following
the provider's caching guide. Put the boundary after the last stable block, not
after per-request content.

**Risk.** Low for behaviour. There is a cost risk: where cache writes are
billed above plain input, a route whose entries are rarely read back costs more
with caching than without. Check that the prompt is above the provider's
minimum cacheable length and that calls arrive within the entry lifetime.

**Verify.** The next usage log shows cache reads well above cache writes on the
route.

## 4. Reuse responses to identical requests

**When.** `duplicate-requests`.

**Cite.** Repeated calls out of total calls, and the prompt and output tokens
the repeats used.

**Change.** Confirm with the user that the repeats are not deliberate (retries
after an error, several samples for variety). Then cache the response in the
application, keyed on a hash of the full request, with an expiry that fits how
quickly a correct answer goes stale.

**Risk.** Medium. Users can receive an out-of-date answer, and a cached bad
answer repeats. Do not cache responses that depend on anything outside the
request, such as the current time or a tool result.

**Verify.** Call count and tokens on the route fall by about the measured
repeat count.

## 5. Bound the output

**When.** `output-heavy`, or output dominating a route's cost in the priced
report.

**Cite.** The route's output tokens, and the median and largest output per
call.

**Change.** Ask for the length and format the consumer of the output actually
needs. Set the output token cap near the largest legitimate response. Where
the provider bills hidden reasoning or thinking tokens as output, check whether
the route needs the setting it uses.

**Risk.** Medium. A cap that is too low truncates answers. A shorter format can
drop content users relied on. Compare outputs before and after on real inputs.

**Verify.** Median output per call falls, and the rate of responses that stop
at the cap does not rise.

## 6. Trim the input

**When.** A route's uncached input stays large after caching works: long
retrieved context, long history, unused tool definitions, repeated examples.

**Cite.** The route's uncached input tokens per call after items 2 and 3.

**Change.** Retrieve fewer or shorter passages, drop tools the route never
calls, shorten or summarise old history, remove examples that no longer earn
their place.

**Risk.** Medium to high. Every one of these removes information the model
used. Run the route's eval, or build a small one from logged inputs, before and
after.

**Verify.** Uncached input per call falls and the eval result holds.

## 7. Move work that can wait to a batch tier

**When.** A route's output is not read by a person in real time: nightly jobs,
backfills, offline classification.

**Cite.** The route's total tokens.

**Change.** Send these requests through the provider's batch or asynchronous
interface where one exists. Take the current discount and turnaround time from
the provider's documentation, not from memory.

**Risk.** Results arrive later, and failure handling differs from the
synchronous path.

**Verify.** The provider's invoice or usage export shows the route billed at
the batch rate.

## 8. Change the model or its reasoning settings

**When.** Last, and only when the user asks for it or the earlier items are
exhausted.

**Cite.** The route's measured tokens by type. The price difference must come
from the user's price table.

**Change.** Propose it as an option, never as a done decision. State that
output quality can change, that prompts tuned for one model may need changes
for another, that token counts for the same text can differ between models, and
that the cache starts empty because entries are per model.

**Risk.** High. This is the only item that replaces the thing producing the
answers.

**Verify.** An eval on the route's real traffic, agreed with the user before
the switch, then a new usage log after it.

## What not to do

- Do not recommend a fix for a route you have no measurement for.
- Do not add cache markers to a prompt whose prefix changes on every request.
  That adds write cost and no reads.
- Do not quote a saving in money without the user's price table.
- Do not shorten a system prompt to save tokens when caching would make the
  same tokens cheap without changing behaviour.
- Do not treat one day's log as the steady state if traffic varies by weekday
  or season. Say what period the log covers.
