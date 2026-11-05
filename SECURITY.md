# Security

An agent skill is instructions and code that your coding agent will follow and run. Review a skill before installing it, the same way you would review any dependency.

## What this skill does on your machine

| | |
|---|---|
| Reads | The usage logs, request-body captures and price table whose paths you pass on the command line |
| Writes | Nothing. Both scripts print to standard output and standard error only |
| Executes | Nothing. The scripts start no other programs |
| Network | None. The bundled scripts import no network modules. `tools/validate.py` checks this in CI |
| Dependencies | Python standard library only |
| Pre-approved tools | None. `SKILL.md` has no `allowed-tools` entry, so your agent's normal permission prompts apply |
| Hooks, MCP servers, background processes | None |

The skill instructs the agent to propose changes to prompt-assembly code only after measuring. Those edits go through your agent's normal review and permission flow.

## Your logs can contain sensitive data

- **Usage logs** hold token counts, route names and model names. `usage_report.py` prints route and model names and counts. It does not need prompt or response text, so do not log that text for this purpose.
- **Captured request bodies** hold full prompts: system instructions, user messages, retrieved documents, and anything else your application sends. `prefix_stability.py` prints a short snippet (up to 30 characters each side) around the first point where requests differ. When an agent runs the script, that snippet enters the agent's context and is sent to the agent's model provider. Capture bodies from a test environment or redact them if that is not acceptable.
- Keep captures out of version control. Delete them when the audit is done.
- If a request body contains a credential, treat that as an incident in its own right: credentials do not belong in prompts.

## Prices

The skill ships no real prices. `assets/prices.example.json` contains made-up numbers and is marked as a placeholder; the report labels any cost computed from it. Cost figures are only as correct as the price table you supply.

## Reporting a vulnerability

Please report security problems privately through the repository's **Security** tab ("Report a vulnerability"). If that option is not available, open an issue that says a private channel is needed, without including exploit details.

## Verifying what you install

- Install from a tagged release and read the diff between tags before updating.
- The whole skill is the `skills/llm-cost-audit/` folder: one Markdown protocol, two Python scripts, one price table template, three reference files, and eval fixtures.
