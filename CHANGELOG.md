# Changelog

All notable changes to this skill are recorded here. Versions follow [Semantic Versioning](https://semver.org).

## 0.1.0 - 2026-11-06

### Added

- `SKILL.md` with the audit protocol: seven hard rules and a seven-step workflow.
- `scripts/usage_report.py` for per-route and per-model token totals, cache-read share, ranked waste findings, and cost from a user-supplied price table.
- `scripts/prefix_stability.py` for the shared prefix of captured request bodies and the first point where they diverge.
- `assets/prices.example.json`, a price table template with placeholder numbers.
- References for log formats, prompt caching, and a savings playbook.
- Three fixture sets (a broken cache, mixed waste, a healthy log) with eval cases, plus trigger queries.
- Unit tests, a format validator, and a CI workflow.
