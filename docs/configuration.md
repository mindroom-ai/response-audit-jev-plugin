# Configuration and Evidence Reference

[Back to README](../README.md)

## Default Checks

| Check | What it flags |
| --- | --- |
| `citations` | Clearly missing required citations or attribution contradicted by the supplied tool evidence |
| `source_use` | Clearly missing required source consultation or lookup claims contradicted by observed tool activity |

The source-use check runs only when `source_tools` is nonempty.
The rubrics distinguish fresh verification from ordinary conversation, creative work, code-only answers, and answers based on data supplied by the user.
A successful tool call is evidence that a function returned, not proof that every claim in its result is correct.
The judge also inspects the result and relevance to the request.

## Custom Checks

Providing `checks` **replaces** the defaults.
Each check asks whether a specific actionable problem is present; a positive judgment triggers its fixed `feedback` text.
The judge does not generate correction prose.

```yaml
plugins:
  - path: ./plugins/response-audit-jev-plugin
    settings:
      agents: [research]
      checks:
        - id: units
          instructions: >-
            Does the answer give physical measurements without the units needed
            to interpret them? Do not flag dimensionless quantities.
          feedback: Add the missing measurement units and verify their consistency.
```

| Setting | Type | Default | Meaning |
| --- | --- | --- | --- |
| `agents` | list of names | `[]` | Agents enabled for audits; maximum 100 |
| `source_tools` | list of function names | `[]` | Known source-access functions; maximum 100 |
| `judgment` | object | TypeSafe, threshold `0.9`, timeout `1.5` seconds | Existing MindRoom judgment configuration |
| `checks` | list | Citation and source-use checks | Between 1 and 8 checks, with unique IDs |
| `checks[].id` | string | Required | Check identifier, 1–100 letters, digits, underscores or hyphens |
| `checks[].instructions` | string | Required | Violation question, 1–2,000 characters |
| `checks[].feedback` | string | Required | Correction guidance, 1–500 characters |
| `checks[].requires_source_tools` | boolean | `false` | Skip this check unless source function names are configured |

Unknown settings are rejected when hooks validate their configuration.
Agent and source function names accept 1–100 letters, digits, underscores or hyphens.
Changing check settings takes effect through MindRoom's normal plugin config reload.
To use an LLM, replace `judgment` with an existing alias under `models`:

```yaml
judgment:
  provider: llm
  model: cheap
  timeout_seconds: 5
```

Each configured check makes one sequential judgment call.
There is no automatic backend fallback or application-level retry.
LLM judgments return booleans; the probability threshold applies only to TypeSafe.
Both backends reuse MindRoom's shared per-agent and process capacity limits.
The after-response hook has a 30-second total timeout; each backend call also has its configured deadline.

## Evidence and Limits

The judge receives the inbound request, the final delivered answer, configured source tool names, and observed tool names, statuses, arguments, and results **from that agent's current turn**.
These contents are sent to the configured external judgment provider.
No system prompt, memory, or complete conversation history is added.
Credentials detected by MindRoom's redaction rules cause the entire audit to be skipped, rather than sending an altered evidence set.
Those rules are not a general personal-data filter; message and tool contents may contain sensitive information.
Only enable this plugin where that data transfer is appropriate.

The plugin watches `message:enrich`, `tool:after_call`, `message:after_response`, and `message:cancelled`.
It uses public hooks and adds no new core hook or agent tool.
It does not browse citations itself and cannot prove factual correctness.
Earlier conversation sources, provider-native tools, delegated work, and tool activity outside the observed turn are not fully visible.
The rubrics treat these limitations as uncertainty, not automatic failure.

Audits are skipped for attachments, missing turn capture, unsupported non-JSON tool results, detected secrets, oversized input, and non-AI/empty responses.
Evidence is never silently truncated: at most 32 tool observations and a 16 KB shared judgment request.
Only 128 pending turn captures are kept in memory, expiring after one hour.
Restart, hot reload, expiry, or eviction can lose pending evidence; such a response is skipped.
Cancelled or failed replies are not audited.

Only successful affirmative issue judgments produce feedback.
Timeouts, missing credentials, capacity exhaustion, abstentions and failed checks produce no finding.
Other successful checks can still contribute findings, provided the total hook deadline has not expired.
Feedback is routed through MindRoom's existing authorization and dispatch rules, preserving the original requester.
The message includes `com.mindroom.response_audit` metadata identifying the audited response event and failed check IDs.

A local SQLite ledger stores only agent, room and response-event identifiers under the plugin's MindRoom state directory.
It claims an audit before inference, ensuring **at most one attempt per response**, including replay and restart.
Delivery is best effort: a crash, timeout, or send failure can lose a correction, and the plugin does not retry it.
The ledger is retained until its state directory is removed; it stores no message bodies or tool results.

## Development

This is a directory plugin, matching other repositories in the MindRoom organization; no separate package installation is required.
With a sibling MindRoom checkout and its development dependencies installed:

```bash
uv run --project ../mindroom pytest tests
uv run --project ../mindroom mindroom plugins check .
uv run --project ../mindroom pre-commit run --all-files
```

On NixOS, run these inside the MindRoom `shell.nix` environment.
CI uses MindRoom's reusable plugin compatibility workflow pinned to `v2026.9.232`.
Tests exercise real hook contexts and mock the provider/network boundary; they do not establish live judgment accuracy or a calibrated threshold.
