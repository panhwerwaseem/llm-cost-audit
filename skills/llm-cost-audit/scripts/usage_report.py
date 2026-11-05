#!/usr/bin/env python3
"""Report token spend and prompt-cache use from LLM usage logs.

Reads JSONL usage records (one JSON object per line), maps each provider's
usage object onto one neutral shape, and prints per-route and per-model totals,
the cache-read share, and ranked waste findings. Cost is printed only when a
price table is passed with --prices. No prices are built in.

Usage shapes recognised (field by field in references/log-format.md):
  neutral             input_tokens, cache_read_tokens, cache_write_tokens, output_tokens
  Anthropic Messages  input_tokens, cache_creation_input_tokens, cache_read_input_tokens
  OpenAI Chat         prompt_tokens, prompt_tokens_details.cached_tokens, completion_tokens
  OpenAI Responses    input_tokens, input_tokens_details.cached_tokens, output_tokens
  Gemini              promptTokenCount, cachedContentTokenCount, candidatesTokenCount

Examples:
  usage_report.py usage.jsonl
  usage_report.py usage.jsonl --prices my-prices.json
  usage_report.py day1.jsonl day2.jsonl --route-field metadata.feature --json

Exit codes: 0 = no finding at or above --fail-on, 1 = at least one finding at
or above --fail-on, 2 = usage or input error.
Standard library only. Reads the files you name. Writes nothing to disk.
Makes no network calls.
"""
import argparse
import json
import sys
from statistics import median

SEVERITIES = ("high", "medium", "info")
TOKEN_KEYS = ("input", "cache_write", "cache_read", "output")
UNIT = "per_million_tokens"
OUTPUT_RATIO = 1.0      # output-heavy: output tokens >= this multiple of prompt tokens
DUPLICATE_SHARE = 0.05  # duplicate-requests: repeats >= this share of a route's calls
CONCENTRATION = 0.5     # spend-concentration: top route >= this share, with 3+ routes
NEXT = {
    "cache-writes-never-read": (
        "Run prefix_stability.py on captured request bodies for this route to find what changes "
        "before the cache breakpoint. If nothing can be made stable, stop writing to the cache here."),
    "uncached-long-input": (
        "Capture request bodies for this route and run prefix_stability.py. Only the prefix the "
        "requests share can become cache reads, so that measurement, not this total, is the saving."),
    "duplicate-requests": (
        "Confirm the repeats are not deliberate (retries after an error, sampling for variety). If "
        "one response can serve an identical request, add a response cache keyed on the request hash."),
    "output-heavy": (
        "Prompt caching does not reduce output tokens. Review the output cap and the length and "
        "format the prompt asks for."),
    "spend-concentration": (
        "Audit this route first. Savings on the other routes are bounded by the remaining share."),
}


class InputError(Exception):
    """A problem with the files the user passed. Reported without a traceback."""


def lookup(obj, dotted):
    """Read a dotted path such as metadata.feature from nested dicts."""
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def tokens(usage, key):
    """Return usage[key] as a non-negative int. A missing or null field is 0."""
    value = usage.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value < 0 or value != int(value):
        raise ValueError("%s must be a non-negative integer, got %r" % (key, value))
    return int(value)


def sub(usage, key):
    value = usage.get(key)
    return value if isinstance(value, dict) else {}


def uncached_part(usage, total_key, cached):
    """For shapes whose prompt count includes cached tokens: return the uncached part."""
    total = tokens(usage, total_key)
    if cached > total:
        raise ValueError("cached tokens (%d) exceed %s (%d)" % (cached, total_key, total))
    return total - cached


def normalise(record, route_field="route"):
    """Map one log record onto the neutral shape. Raises ValueError if it cannot.

    In the result, input + cache_read + cache_write is the whole prompt: "input"
    holds only the tokens that were neither read from nor written to a cache.
    """
    if not isinstance(record, dict):
        raise ValueError("record is not a JSON object")
    usage = record.get("usage")
    if not isinstance(usage, dict):
        usage = record.get("usageMetadata")
    if not isinstance(usage, dict):
        usage = record
    cache_write = cache_write_1h = 0
    if "prompt_tokens" in usage:
        shape = "openai-chat"
        cache_read = tokens(sub(usage, "prompt_tokens_details"), "cached_tokens")
        uncached = uncached_part(usage, "prompt_tokens", cache_read)
        output = tokens(usage, "completion_tokens")
    elif "promptTokenCount" in usage:
        shape = "gemini"
        cache_read = tokens(usage, "cachedContentTokenCount")
        uncached = uncached_part(usage, "promptTokenCount", cache_read)
        output = tokens(usage, "candidatesTokenCount") + tokens(usage, "thoughtsTokenCount")
    elif "input_tokens_details" in usage:
        shape = "openai-responses"
        cache_read = tokens(sub(usage, "input_tokens_details"), "cached_tokens")
        uncached = uncached_part(usage, "input_tokens", cache_read)
        output = tokens(usage, "output_tokens")
    elif "input_tokens" in usage or "output_tokens" in usage:
        uncached = tokens(usage, "input_tokens")
        output = tokens(usage, "output_tokens")
        if "cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage:
            shape = "anthropic"
            cache_read = tokens(usage, "cache_read_input_tokens")
            cache_write = tokens(usage, "cache_creation_input_tokens")
            cache_write_1h = tokens(sub(usage, "cache_creation"), "ephemeral_1h_input_tokens")
        else:
            shape = "neutral"
            cache_read = tokens(usage, "cache_read_tokens")
            cache_write = tokens(usage, "cache_write_tokens")
    else:
        raise ValueError("no recognised token fields (see references/log-format.md)")
    route = lookup(record, route_field)
    request_hash = record.get("request_hash")
    return {
        "route": "(no route)" if route is None else str(route),
        "model": str(record.get("model") or record.get("modelVersion") or "(unknown)"),
        "shape": shape, "input": uncached, "cache_read": cache_read, "cache_write": cache_write,
        "cache_write_1h": cache_write_1h, "output": output,
        "request_hash": None if request_hash is None else str(request_hash),
    }


def read_logs(paths, route_field):
    """Return (records, problems). Each record carries ref = (file, line number)."""
    records, problems = [], []
    for path in paths:
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError as exc:
            raise InputError("cannot read %s: %s" % (path, exc.strerror or exc))
        with handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = normalise(json.loads(line), route_field)
                except ValueError as exc:  # includes json.JSONDecodeError
                    what = "not valid JSON: " if isinstance(exc, json.JSONDecodeError) else ""
                    problems.append("%s:%d: %s%s" % (path, number, what, exc))
                    continue
                record["ref"] = (path, number)
                records.append(record)
    return records, problems


def load_prices(path):
    """Load and check a price table. Prices are per million tokens."""
    try:
        with open(path, encoding="utf-8") as handle:
            table = json.load(handle)
    except OSError as exc:
        raise InputError("cannot read %s: %s" % (path, exc.strerror or exc))
    except ValueError as exc:
        raise InputError("%s is not valid JSON: %s" % (path, exc))
    if not isinstance(table, dict) or table.get("unit") != UNIT:
        raise InputError('%s must be a JSON object with "unit": "%s"' % (path, UNIT))
    models = table.get("models")
    if not isinstance(models, dict) or not models:
        raise InputError('%s needs a non-empty "models" object' % path)
    for name, entry in sorted(models.items()):
        if not isinstance(entry, dict) or "input" not in entry or "output" not in entry:
            raise InputError('%s: models.%s must be an object with "input" and "output" prices'
                             % (path, name))
        for key in TOKEN_KEYS:
            value = entry.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise InputError("%s: models.%s.%s must be a number >= 0" % (path, name, key))
    return {"file": path, "currency": str(table.get("currency") or "unspecified currency"),
            "placeholder": bool(table.get("placeholder")), "unit": UNIT, "models": models}


def new_totals():
    return {"calls": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0}


def add(totals, record):
    totals["calls"] += 1
    for key in TOKEN_KEYS:
        totals[key] += record[key]


def prompt_tokens(totals):
    return totals["input"] + totals["cache_read"] + totals["cache_write"]


def all_tokens(totals):
    return prompt_tokens(totals) + totals["output"]


def cache_read_share(totals):
    """Cache-read tokens as a share of all prompt tokens. None when there are none."""
    prompt = prompt_tokens(totals)
    return round(totals["cache_read"] / float(prompt), 4) if prompt else None


def cost_of(totals, price, model):
    """Cost of a token total at one model's prices (per million tokens)."""
    cost = 0.0
    for key in TOKEN_KEYS:
        if totals[key] and key not in price:
            raise InputError('the log has %s tokens for model "%s" but its price entry has no '
                             '"%s" price' % (key, model, key))
        cost += totals[key] * price.get(key, 0) / 1e6
    return cost


def aggregate(records):
    """Group records by (route, model) and keep what the findings need."""
    groups = {}
    for record in records:
        key = (record["route"], record["model"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "route": key[0], "model": key[1], "totals": new_totals(),
                "prompts": [], "outputs": [], "refs": [],
                "seen": set(), "repeated": set(), "repeats": new_totals(), "repeat_refs": [],
            }
        add(group["totals"], record)
        group["prompts"].append(record["input"] + record["cache_read"] + record["cache_write"])
        group["outputs"].append(record["output"])
        if len(group["refs"]) < 3:
            group["refs"].append(record["ref"])
        digest = record["request_hash"]
        if digest is not None and digest in group["seen"]:
            add(group["repeats"], record)
            group["repeated"].add(digest)
            if len(group["repeat_refs"]) < 3:
                group["repeat_refs"].append(record["ref"])
        elif digest is not None:
            group["seen"].add(digest)
    return groups


def rollup(groups, index, costs):
    """Sum group totals by route (index 0) or by model (index 1), largest first."""
    rows = {}
    for key in sorted(groups):
        row = rows.setdefault(key[index], dict(new_totals(), name=key[index]))
        for field in ("calls",) + TOKEN_KEYS:
            row[field] += groups[key]["totals"][field]
        if costs is not None:
            row["cost"] = round(row.get("cost", 0.0) + costs[key], 6)
    for row in rows.values():
        row["cache_read_share"] = cache_read_share(row)
    return sorted(rows.values(), key=lambda row: (-weight(row), row["name"]))


def weight(row):
    """What a row is ranked by: cost when prices were given, otherwise all tokens."""
    return row["cost"] if "cost" in row else all_tokens(row)


def show_refs(refs):
    """Render [(file, line), ...] as "file lines 1, 3, 5", one clause per file."""
    files = []
    for path, _ in refs:
        if path not in files:
            files.append(path)
    return "; ".join("%s line%s %s" % (
        path, "" if sum(1 for p, _ in refs if p == path) == 1 else "s",
        ", ".join(str(line) for p, line in refs if p == path)) for path in files)


def find_waste(groups, routes, prices, min_calls, min_input_tokens):
    """Return findings, most severe first, then by cost (or tokens) involved."""
    found = []

    def report(severity, rule, group, kind, involved, summary, refs=None, records=None):
        """involved is a totals dict holding only the tokens this finding is about."""
        price = prices["models"][group["model"]] if prices else None
        found.append({
            "severity": severity, "id": rule, "route": group["route"], "model": group["model"],
            "tokens_involved": all_tokens(involved), "token_kind": kind,
            "cost_involved": round(cost_of(involved, price, group["model"]), 6) if price else None,
            "summary": summary, "next": NEXT[rule],
            "evidence": {"records": group["totals"]["calls"] if records is None else records,
                         "first_at": show_refs(group["refs"] if refs is None else refs)},
        })

    for key in sorted(groups):
        group = groups[key]
        totals, calls, repeats = group["totals"], group["totals"]["calls"], group["repeats"]
        prompt = prompt_tokens(totals)
        median_prompt = int(median(group["prompts"]))

        def just(field):
            return dict(new_totals(), **{field: totals[field]})

        if calls >= 2 and totals["cache_write"] and not totals["cache_read"]:
            report("high", "cache-writes-never-read", group, "cache-write tokens", just("cache_write"),
                   "%d calls wrote %s tokens to the prompt cache and read 0 back."
                   % (calls, fmt(totals["cache_write"])))
        if (calls >= min_calls and not totals["cache_read"] and not totals["cache_write"]
                and median_prompt >= min_input_tokens):
            report("medium", "uncached-long-input", group, "uncached input tokens", just("input"),
                   "%d calls with a median prompt of %s tokens and no cache reads or writes. All "
                   "%s input tokens were billed as uncached input."
                   % (calls, fmt(median_prompt), fmt(totals["input"])))
        if repeats["calls"] >= 2 and repeats["calls"] >= DUPLICATE_SHARE * calls:
            report("medium", "duplicate-requests", group, "tokens spent on repeated requests", repeats,
                   "%d of %d calls repeat a request_hash already seen on this route (%d distinct "
                   "requests were repeated). The repeats used %s prompt and %s output tokens."
                   % (repeats["calls"], calls, len(group["repeated"]),
                      fmt(prompt_tokens(repeats)), fmt(repeats["output"])),
                   refs=group["repeat_refs"], records=repeats["calls"])
        if calls >= min_calls and totals["output"] and totals["output"] >= OUTPUT_RATIO * prompt:
            report("info", "output-heavy", group, "output tokens", just("output"),
                   "Output is %s the prompt size on this route (%s output against %s prompt tokens). "
                   "Median output per call %s, largest %s."
                   % ("%.1fx" % (totals["output"] / float(prompt)) if prompt else "larger than",
                      fmt(totals["output"]), fmt(prompt),
                      fmt(int(median(group["outputs"]))), fmt(max(group["outputs"]))))
    grand = sum(weight(row) for row in routes)
    if len(routes) >= 3 and grand and weight(routes[0]) >= CONCENTRATION * grand:
        top = routes[0]
        refs = [ref for key in sorted(groups) if key[0] == top["name"] for ref in groups[key]["refs"]]
        found.append({
            "severity": "info", "id": "spend-concentration", "route": top["name"], "model": None,
            "tokens_involved": all_tokens(top), "token_kind": "tokens on this route",
            "cost_involved": top.get("cost"),
            "summary": "%s accounts for %.1f%% of %s across %d routes." % (
                top["name"], 100.0 * weight(top) / grand,
                "cost" if "cost" in top else "all tokens", len(routes)),
            "next": NEXT["spend-concentration"],
            "evidence": {"records": top["calls"], "first_at": show_refs(sorted(refs)[:3])},
        })
    by_cost = prices is not None
    found.sort(key=lambda f: (SEVERITIES.index(f["severity"]),
                              -(f["cost_involved"] if by_cost else f["tokens_involved"]),
                              f["id"], f["route"], f["model"] or ""))
    return found


def build_report(paths, records, problems, prices, min_calls=5, min_input_tokens=1000):
    groups = aggregate(records)
    costs = None
    if prices is not None:
        missing = sorted(set(g["model"] for g in groups.values()) - set(prices["models"]))
        if missing:
            raise InputError(
                "%s has no price entry for these models in the log: %s. Model names must match "
                "exactly. Add them, or run without --prices for token analysis only."
                % (prices["file"], ", ".join('"%s"' % name for name in missing)))
        costs = dict((key, cost_of(g["totals"], prices["models"][g["model"]], g["model"]))
                     for key, g in groups.items())
    routes = rollup(groups, 0, costs)
    notes = []
    if problems:
        notes.append("%d line(s) skipped as invalid: %s%s" % (
            len(problems), "; ".join(problems[:3]), " ..." if len(problems) > 3 else ""))
    if not any(record["request_hash"] is not None for record in records):
        notes.append("duplicate-request check skipped: no record has a request_hash field.")
    long_writes = sum(record["cache_write_1h"] for record in records)
    if long_writes and prices is not None:
        notes.append("%s cache-write tokens used the 1-hour lifetime (usage.cache_creation."
                     "ephemeral_1h_input_tokens). Every cache write is priced at the one "
                     "cache_write price here, so check that cost by hand." % fmt(long_writes))
    shapes = {}
    for record in records:
        shapes[record["shape"]] = shapes.get(record["shape"], 0) + 1
    return {
        "files": paths, "records": len(records), "shapes": shapes,
        "prices": None if prices is None else {k: v for k, v in prices.items() if k != "models"},
        "notes": notes, "routes": routes, "models": rollup(groups, 1, costs),
        "findings": find_waste(groups, routes, prices, min_calls, min_input_tokens),
    }


def fmt(number):
    return "{:,}".format(number)


def money(amount):
    """Two decimals, or six for amounts too small to show in two."""
    return "{:,.2f}".format(amount) if amount == 0 or amount >= 0.01 else "{:.6f}".format(amount)


def print_table(title, rows, cost_label):
    headers = [title, "calls", "uncached in", "cache write", "cache read", "output", "cache-read share"]
    total = dict(new_totals(), name="total")
    for row in rows:
        for field in ("calls",) + TOKEN_KEYS:
            total[field] += row[field]
    total["cache_read_share"] = cache_read_share(total)
    if cost_label:
        headers.append(cost_label)
        total["cost"] = sum(row["cost"] for row in rows)
    lines = [headers]
    for row in rows + [total]:
        share = row["cache_read_share"]
        lines.append([row["name"]] + [fmt(row[field]) for field in ("calls",) + TOKEN_KEYS]
                     + ["n/a" if share is None else "%.1f%%" % (share * 100)]
                     + ([money(row["cost"])] if cost_label else []))
    widths = [max(len(line[i]) for line in lines) for i in range(len(headers))]
    for line in lines:
        cells = [line[0].ljust(widths[0])] + [line[i].rjust(widths[i]) for i in range(1, len(line))]
        print("  ".join(cells).rstrip())
    print("")


def print_text(report):
    prices = report["prices"]
    print("records: %s from %d file(s)   routes: %d   models: %d   usage shapes: %s" % (
        fmt(report["records"]), len(report["files"]), len(report["routes"]), len(report["models"]),
        ", ".join("%s x%d" % item for item in sorted(report["shapes"].items()))))
    cost_label = None
    if prices is None:
        print("cost: not computed. No price table was given (--prices), so this is token "
              "analysis only.")
    else:
        cost_label = "cost (%s)" % ("PLACEHOLDER" if prices["placeholder"] else prices["currency"])
        print("cost: from %s, per million tokens, in %s." % (prices["file"], prices["currency"]))
        if prices["placeholder"]:
            print("PLACEHOLDER PRICES: %s is marked as a placeholder. The cost figures below are "
                  "not real. Replace the numbers with current prices from your provider."
                  % prices["file"])
    for note in report["notes"]:
        print("note: " + note)
    print("")
    print_table("route", report["routes"], cost_label)
    print_table("model", report["models"], cost_label)
    if not report["findings"]:
        print("Findings: none.")
        return
    print("Findings, most severe first, then by %s involved:"
          % ("tokens" if prices is None else "cost"))
    for rank, item in enumerate(report["findings"], 1):
        print("%d. [%s] %s  route=%s%s" % (
            rank, item["severity"], item["id"], item["route"],
            " model=%s" % item["model"] if item["model"] else ""))
        print("   " + item["summary"])
        print("   involved: %s %s%s" % (
            fmt(item["tokens_involved"]), item["token_kind"],
            "" if item["cost_involved"] is None
            else ", %s at the table prices" % money(item["cost_involved"])))
        print("   evidence: %d record(s), first at %s" % (
            item["evidence"]["records"], item["evidence"]["first_at"]))
        print("   next: " + item["next"])


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="+", metavar="LOG.jsonl", help="usage log files (JSONL)")
    parser.add_argument("--prices", metavar="FILE",
                        help="price table JSON in the format of assets/prices.example.json; "
                             "without it no cost is printed")
    parser.add_argument("--route-field", default="route", metavar="PATH",
                        help="record field that names the route; dotted paths allowed (default: route)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--fail-on", choices=SEVERITIES + ("never",), default="medium",
                        help="exit 1 when a finding is at or above this severity (default: medium)")
    parser.add_argument("--min-calls", type=int, default=5,
                        help="calls a route needs before the long-input and output-heavy rules "
                             "apply (default 5)")
    parser.add_argument("--min-input-tokens", type=int, default=1000,
                        help="median prompt tokens per call that count as a long input (default 1000)")
    parser.add_argument("--skip-invalid", action="store_true",
                        help="leave out lines that cannot be read instead of stopping")
    args = parser.parse_args(argv)
    if args.min_calls < 1 or args.min_input_tokens < 0:
        parser.error("--min-calls must be at least 1 and --min-input-tokens at least 0")
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        records, problems = read_logs(args.logs, args.route_field)
        if problems and not args.skip_invalid:
            raise InputError("%d line(s) could not be read:\n  %s%s\nFix them, or pass "
                             "--skip-invalid to leave them out of the report." % (
                                 len(problems), "\n  ".join(problems[:10]),
                                 "\n  ..." if len(problems) > 10 else ""))
        if not records:
            raise InputError("no usage records found in %s" % ", ".join(args.logs))
        prices = load_prices(args.prices) if args.prices else None
        report = build_report(args.logs, records, problems, prices,
                              args.min_calls, args.min_input_tokens)
    except InputError as exc:
        print("usage_report.py: error: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_text(report)
    if args.fail_on == "never":
        return 0
    limit = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f["severity"]) <= limit for f in report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
