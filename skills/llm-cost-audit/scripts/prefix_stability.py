#!/usr/bin/env python3
"""Find where captured LLM request bodies stop sharing a prefix.

A prompt cache matches on the start of the prompt. One value that changes near
the top (a timestamp, a request ID, a reordered JSON key) stops everything after
it from being read from the cache. This script takes captured request bodies,
groups them by route and model, and reports how much leading prompt content
every request in a group shares, where the first difference is, and what the
differing text looks like.

Input is JSONL, one request per line, in either form:
  {"route": "support-chat", "body": {<request body as sent to the provider>}}
  {<request body>, "route": "support-chat"}          ("request" also works for "body")

Fields compared, in this order: tools; then system, systemInstruction,
system_instruction, instructions; then messages, input, contents.
cache_control markers are ignored. Other request fields are not compared.

Lengths are counted in characters of the string and number values in those
fields. JSON keys and punctuation are not counted. The token figure is that
count divided by --chars-per-token: a rough estimate, not a tokenizer.

Examples:
  prefix_stability.py requests.jsonl
  prefix_stability.py requests.jsonl --route support-chat
  prefix_stability.py requests.jsonl --json

Exit codes: 0 = no finding at or above --fail-on, 1 = at least one finding at
or above --fail-on, 2 = usage or input error.
Standard library only. Reads the files you name. Writes nothing to disk.
Makes no network calls.
"""
import argparse
import json
import os
import re
import sys
from statistics import median

SEVERITIES = ("high", "medium")
SYSTEM_FIELDS = ("system", "systemInstruction", "system_instruction", "instructions")
CONVERSATION_FIELDS = ("messages", "input", "contents")
SECTIONS = ("tools", "system", "conversation")
SYSTEM_ROLES = ("system", "developer")
CONTEXT_CHARS = 30
VOLATILE_PATTERNS = (
    ("a UUID", re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")),
    ("a timestamp or date", re.compile(
        r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?")),
    ("a clock time", re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")),
    ("a long number or ID", re.compile(r"\b(?:\d{6,}|[0-9a-fA-F]{16,})\b")),
)


class InputError(Exception):
    """A problem with the files the user passed. Reported without a traceback."""


def flatten(value, path, section, out, sort_keys):
    """Append (section, path, text) for every leaf value, in document order."""
    if isinstance(value, dict) and value:
        keys = sorted(value) if sort_keys else list(value)
        for key in keys:
            if key != "cache_control":
                flatten(value[key], "%s.%s" % (path, key), section, out, sort_keys)
    elif isinstance(value, list) and value:
        for index, child in enumerate(value):
            flatten(child, "%s[%d]" % (path, index), section, out, sort_keys)
    else:
        out.append((section, path, value if isinstance(value, str) else json.dumps(value)))


def leaves(body, sort_keys=False):
    """Turn a request body into its ordered prompt content: tools, system, conversation."""
    out = []
    if "tools" in body:
        flatten(body["tools"], "tools", "tools", out, sort_keys)
    for field in SYSTEM_FIELDS:
        if field in body:
            flatten(body[field], field, "system", out, sort_keys)
    for field in CONVERSATION_FIELDS:
        value = body.get(field)
        if isinstance(value, list):
            leading = True  # system-role messages count as system only at the start
            for index, item in enumerate(value):
                role = item.get("role") if isinstance(item, dict) else None
                leading = leading and role in SYSTEM_ROLES
                flatten(item, "%s[%d]" % (field, index),
                        "system" if leading else "conversation", out, sort_keys)
        elif field in body:
            flatten(value, field, "conversation", out, sort_keys)
    return out


def walk(requests, start=0):
    """Compare leaf lists from index start. Return (shared chars, stop index, kind).

    kind is None when every request is identical to the end, "value" when the
    same field holds different text, "structure" when the requests have different
    fields at that position, and "length" when a request ends and others go on.
    """
    chars = 0
    shortest = min(len(request) for request in requests)
    for index in range(start, shortest):
        column = [request[index] for request in requests]
        if len(set(leaf[1] for leaf in column)) > 1:
            return chars, index, "structure"
        if len(set(leaf[2] for leaf in column)) > 1:
            return chars, index, "value"
        chars += len(column[0][2])
    if any(len(request) != shortest for request in requests):
        return chars, max(start, shortest), "length"
    return chars, max(start, shortest), None


def common_suffix(texts, limit):
    """Length of the suffix all texts share, at most limit characters."""
    return min(limit, len(os.path.commonprefix([text[::-1] for text in texts])))


def volatile_hint(texts, offset):
    """Name what the differing text looks like, if every sample fits one pattern."""
    for name, pattern in VOLATILE_PATTERNS:
        if all(any(m.start() <= offset <= m.end() for m in pattern.finditer(text))
               for text in texts):
            return name
    return None


def first_of_each(group, values):
    """Return [(ref, value)] for each distinct value, in order of first appearance."""
    seen, out = set(), []
    for (ref, _), value in zip(group, values):
        if value not in seen:
            seen.add(value)
            out.append((ref, value))
    return out


def snippet(text, offset):
    before = text[max(0, offset - CONTEXT_CHARS):offset]
    after = text[offset:offset + CONTEXT_CHARS]
    lead = "[..]" if offset > CONTEXT_CHARS else ""
    tail = "[..]" if len(text) > offset + CONTEXT_CHARS else ""
    return "%s%s>>>%s%s" % (lead, before, after, tail)


def analyse(group, chars_per_token):
    """Measure one route+model group. group is a list of (ref, body)."""
    requests = [leaves(body) for _, body in group]
    sizes = [sum(len(leaf[2]) for leaf in request) for request in requests]
    shared, index, kind = walk(requests)
    result = {
        "requests": len(group), "median_chars": int(median(sizes)), "status": "ok",
        "divergence": None, "potential_chars": None, "finding": None,
    }
    if kind is None:
        result["status"] = "identical"
    elif kind == "length":
        result["status"] = "append-only"
    else:
        column = [request[index] for request in requests]
        section = min((leaf[0] for leaf in column), key=SECTIONS.index)
        divergence = {"kind": kind, "section": section, "hint": None, "samples": []}
        if kind == "value":
            texts = [leaf[2] for leaf in column]
            offset = len(os.path.commonprefix(texts))
            shared += offset
            variants = first_of_each(group, texts)
            suffix = common_suffix(texts, min(len(text) for text in texts) - offset)
            result["potential_chars"] = shared + suffix + walk(requests, index + 1)[0]
            divergence.update(
                path=column[0][1], offset=offset, distinct=len(variants),
                hint=volatile_hint([text for _, text in variants[:5]], offset),
                samples=[{"ref": ref, "text": snippet(text, offset)} for ref, text in variants[:3]])
        else:
            paths = first_of_each(group, [leaf[1] for leaf in column])
            divergence.update(
                path=paths[0][1], offset=0, distinct=len(paths),
                samples=[{"ref": ref, "text": "next field is %s" % path} for ref, path in paths[:3]])
            if section != "conversation":
                canonical = set(
                    tuple(leaf for leaf in leaves(body, sort_keys=True) if leaf[0] == section)
                    for _, body in group)
                if len(canonical) == 1:
                    divergence["hint"] = "a different JSON key order"
        result["divergence"] = divergence
        result["finding"] = judge(divergence, len(group))
        if result["finding"]:
            result["status"] = "unstable"
    result["shared_chars"] = shared
    result["shared_tokens_estimate"] = int(round(shared / chars_per_token))
    result["shared_share"] = (round(shared / float(result["median_chars"]), 4)
                              if result["median_chars"] else None)
    if result["potential_chars"] is not None:
        result["potential_tokens_estimate"] = int(round(result["potential_chars"] / chars_per_token))
    return result


def judge(divergence, count):
    """Return a finding when requests differ inside tools or the system prompt."""
    if divergence["section"] == "conversation":
        return None
    where = "%s, character %d" % (divergence["path"], divergence["offset"])
    name = "tools section" if divergence["section"] == "tools" else "system prompt"
    if divergence["hint"] == "a different JSON key order":
        return {"severity": "high", "id": "unstable-prefix", "summary": (
            "The %s has the same content in every request but a different JSON key order, "
            "first at %s. A reordered key can render as a different prompt and miss the cache."
            % (name, divergence["path"])),
            "next": "Serialise this section the same way every time (fixed key order)."}
    if divergence["distinct"] == count and count >= 3:
        return {"severity": "high", "id": "unstable-prefix", "summary": (
            "The %s differs in every request (%d of %d) at %s. No request can read another "
            "request's cache entry past that point." % (name, count, count, where)),
            "next": "Move the changing value after the stable content (for example into the "
                    "latest user message), or remove it if the model does not need it."}
    return {"severity": "medium", "id": "unstable-prefix", "summary": (
        "The %s has %d variants across %d requests, first differing at %s. Requests share a "
        "cache only with requests of the same variant." % (name, divergence["distinct"], count, where)),
        "next": "Decide whether the variants are intended (per tenant, per prompt version). If "
                "so, check each variant gets enough traffic to be read back. If not, make the "
                "prefix identical."}


def read_requests(paths, route_field, only_route):
    """Return {(route, model): [(ref, body), ...]} in input order."""
    groups, problems = {}, []
    for path in paths:
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError as exc:
            raise InputError("cannot read %s: %s" % (path, exc.strerror or exc))
        with handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                ref = "%s:%d" % (path, number)
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    problems.append("%s: not valid JSON (%s)" % (ref, exc))
                    continue
                if not isinstance(record, dict):
                    problems.append("%s: line is not a JSON object" % ref)
                    continue
                body = record
                for key in ("body", "request"):
                    if isinstance(record.get(key), dict):
                        body = record[key]
                        break
                if not any(field in body for field in ("tools",) + SYSTEM_FIELDS + CONVERSATION_FIELDS):
                    problems.append("%s: no prompt fields found (expected one of messages, input, "
                                    "contents, system, tools)" % ref)
                    continue
                route = record.get(route_field)
                route = "(no route)" if route is None else str(route)
                if only_route is not None and route != only_route:
                    continue
                model = body.get("model") or record.get("model") or "(unknown)"
                groups.setdefault((route, str(model)), []).append((ref, body))
    if problems:
        raise InputError("%d line(s) could not be read:\n  %s%s" % (
            len(problems), "\n  ".join(problems[:10]), "\n  ..." if len(problems) > 10 else ""))
    return groups


def fmt(number):
    return "{:,}".format(number)


def print_text(report):
    one_file = len(report["files"]) == 1
    for entry in report["routes"]:
        print("route %s   model %s   requests: %d" % (entry["route"], entry["model"], entry["requests"]))
        if entry["status"] == "skipped":
            print("  skipped: at least 2 requests are needed to compare.\n")
            continue
        print("  shared prefix: %s chars (about %s tokens, rough estimate), %s of the median request" % (
            fmt(entry["shared_chars"]), fmt(entry["shared_tokens_estimate"]),
            "n/a" if entry["shared_share"] is None else "%.0f%%" % (entry["shared_share"] * 100)))
        divergence = entry["divergence"]
        if entry["status"] == "identical":
            print("  all %d requests are identical in the compared fields. If one response can "
                  "serve them all, a response cache avoids the repeat calls." % entry["requests"])
        elif entry["status"] == "append-only":
            print("  the shortest request is a complete prefix of the others (a conversation that "
                  "only grows). No difference inside the shared part.")
        else:
            print("  first difference: %s, character %d (section: %s); %d distinct value(s) in "
                  "%d requests%s" % (
                      divergence["path"], divergence["offset"], divergence["section"],
                      divergence["distinct"], entry["requests"],
                      "; looks like %s" % divergence["hint"] if divergence["hint"] else ""))
            for sample in divergence["samples"]:
                where = "line " + sample["ref"].rpartition(":")[2] if one_file else sample["ref"]
                print("    %s  %s" % (where, json.dumps(sample["text"], ensure_ascii=False)))
        finding = entry["finding"]
        if finding:
            if entry["potential_chars"] is not None:
                print("  with that span moved out of the way, the shared prefix would be %s chars "
                      "(about %s tokens, rough estimate)" % (
                          fmt(entry["potential_chars"]), fmt(entry["potential_tokens_estimate"])))
            print("  [%s] %s: %s" % (finding["severity"], finding["id"], finding["summary"]))
            print("  next: " + finding["next"])
        elif entry["status"] == "ok":
            print("  OK: tools and system prompt are identical across requests. The requests "
                  "first differ in the conversation.")
        print("")
    count = len(report["findings"])
    print("Findings: %s" % ("none." if not count else "%d unstable prefix(es)." % count))


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("requests", nargs="+", metavar="REQUESTS.jsonl",
                        help="captured request bodies (JSONL)")
    parser.add_argument("--route", help="analyse only this route")
    parser.add_argument("--route-field", default="route", metavar="NAME",
                        help="top-level field that names the route (default: route)")
    parser.add_argument("--chars-per-token", type=float, default=4.0,
                        help="divisor for the rough token estimate (default 4.0)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--fail-on", choices=SEVERITIES + ("never",), default="medium",
                        help="exit 1 when a finding is at or above this severity (default: medium)")
    args = parser.parse_args(argv)
    if args.chars_per_token <= 0:
        parser.error("--chars-per-token must be above 0")
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        groups = read_requests(args.requests, args.route_field, args.route)
        if not groups:
            raise InputError("no requests found%s in %s" % (
                ' for route "%s"' % args.route if args.route else "", ", ".join(args.requests)))
    except InputError as exc:
        print("prefix_stability.py: error: %s" % exc, file=sys.stderr)
        return 2
    report = {"files": args.requests, "routes": [], "findings": []}
    for (route, model) in sorted(groups):
        group = groups[(route, model)]
        if len(group) < 2:
            entry = {"requests": len(group), "status": "skipped", "finding": None}
        else:
            entry = analyse(group, args.chars_per_token)
        entry.update(route=route, model=model, refs=[ref for ref, _ in group[:3]])
        report["routes"].append(entry)
        if entry["finding"]:
            report["findings"].append(dict(
                entry["finding"], route=route, model=model, path=entry["divergence"]["path"],
                evidence=[sample["ref"] for sample in entry["divergence"]["samples"]]))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_text(report)
    if args.fail_on == "never":
        return 0
    limit = SEVERITIES.index(args.fail_on)
    return 1 if any(SEVERITIES.index(f["severity"]) <= limit for f in report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
