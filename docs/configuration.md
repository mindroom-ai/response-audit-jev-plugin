# Configuration and Evidence Reference

[Back to README](../README.md)

## Default Check

| Check | What it flags |
| --- | --- |
| `research` | Checkable facts a person would act on, such as what a place, company, document, law, or study is, says, costs, or reports, that the recorded tool calls did not look up or do not support |

Facts the person supplied in the audited message or in shared files, general advice about something the person named, stable common knowledge, opinions, and pointers to where information can be found do not count.
A successful tool call is evidence that a function returned, not proof that every claim in its result is correct.
The judge also inspects the result and relevance to the request.

## Custom Checks

Providing `checks` **replaces** the defaults.
Each check asks whether a specific actionable problem is present; a positive judgment triggers its fixed `feedback` text.
Without `when_true` and `when_false`, the judge answers whether a clear, actionable violation is supported by the evidence.
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
| `judgment` | object | TypeSafe, threshold `0.9`, timeout `1.5` seconds | Existing MindRoom judgment configuration |
| `checks` | list | The `research` check | Between 1 and 8 checks, with unique IDs |
| `checks[].id` | string | Required | Check identifier, 1–100 letters, digits, underscores or hyphens |
| `checks[].instructions` | string | Required | Violation question, 1–2,000 characters |
| `checks[].when_true` | string | A clear, actionable violation is supported by the evidence | When the judge should answer yes, 1–2,000 characters |
| `checks[].when_false` | string | No clear violation, not applicable, or insufficient evidence | When the judge should answer no, 1–2,000 characters |
| `checks[].feedback` | string | Required | Correction guidance, 1–500 characters |

Unknown settings are rejected when hooks validate their configuration.
When upgrading from a version with the `citations` and `source_use` checks, remove `source_tools` and any `requires_source_tools` from the plugin settings, or every audit fails validation.
Agent names accept 1–100 letters, digits, underscores or hyphens.
Changing check settings takes effect through MindRoom's normal plugin config reload.
To use the OpenAI Decisions API, set `judgment: {provider: openai_decisions, timeout_seconds: 5}`; it uses the instance's OpenAI API key, and its default threshold is `0.8`.
The evaluation used a 5-second or longer timeout: slow periods exceed the 1.5-second default, and a timeout produces no finding.
To use an LLM, replace `judgment` with an existing alias under `models`:

```yaml
judgment:
  provider: llm
  model: cheap
  timeout_seconds: 5
```

Each configured check makes one sequential judgment call.
There is no automatic backend fallback or application-level retry.
LLM judgments return booleans; the probability threshold applies to TypeSafe and OpenAI Decisions.
All backends reuse MindRoom's shared per-agent and process capacity limits.
The after-response hook has a 30-second total timeout; each backend call also has its configured deadline.

## Evidence and Limits

The judge receives the inbound request, the final delivered answer, the names of files and images shared in the conversation, and observed tool names, statuses, arguments, and results **from that agent's current turn**.
Shared files reach the model without a tool call, so facts from them count as supplied by the person; voice-message audio is not listed because its transcript is the request.
When voice transcription is off or fails, the agent hears the raw audio while the judge sees only a placeholder request, so facts the person spoke can draw a follow-up.
These contents are sent to the configured external judgment provider.
No system prompt, memory, or complete conversation history is added.
Credentials detected by MindRoom's redaction rules cause the entire audit to be skipped, rather than sending an altered evidence set.
Those rules are not a general personal-data filter; message and tool contents may contain sensitive information.
Only enable this plugin where that data transfer is appropriate.

The plugin watches `message:enrich`, `tool:after_call`, `message:after_response`, and `message:cancelled`.
It uses public hooks and adds no new core hook or agent tool.
It does not browse citations itself and cannot prove factual correctness.
Earlier conversation sources, provider-native tools, delegated work, and tool activity outside the observed turn are not fully visible.
The judge sees only the audited message, the names of shared files, and this turn's tool calls, so facts from earlier messages or lookups in the thread, the agent's instructions, context files, or memory, provider-native tools, or delegated work can draw a follow-up, and a fact added beside a shared file can be credited to that file.

Audits are skipped for missing turn capture, unsupported non-JSON tool results, detected secrets, oversized input, non-AI/empty responses, and turns no person asked for, such as schedules, webhooks, and messages from agents or plugins.
Evidence is never silently truncated: at most 32 tool observations and a 16 KB shared judgment request.
Only 128 pending turn captures are kept in memory, expiring after one hour.
Restart, hot reload, expiry, or eviction can lose pending evidence; such a response is skipped.
Cancelled or failed replies are not audited.

Only successful affirmative issue judgments produce feedback.
Timeouts, missing credentials, capacity exhaustion, abstentions and failed checks produce no finding.
Other successful checks can still contribute findings, provided the total hook deadline has not expired.
Feedback is routed through MindRoom's existing authorization and dispatch rules, preserving the original requester.
The message quotes the start of the audited answer, which names that answer even when newer replies follow it, and includes `com.mindroom.response_audit` metadata identifying the audited response event and failed check IDs.

A local SQLite ledger stores only agent, room and response-event identifiers under the plugin's MindRoom state directory.
It claims an audit before inference, ensuring **at most one attempt per response**, including replay and restart.
Delivery is best effort: a crash, timeout, or send failure can lose a correction, and the plugin does not retry it.
The ledger is retained until its state directory is removed; it stores no message bodies or tool results.

## Evaluation

The default check was chosen on 38 labeled single-message exchanges: unresearched recommendations and corporate claims, replies backed by searches, page reads, or shared files, and replies with no factual claims.
A reply counts as handled correctly when it gets a follow-up exactly when it states facts no lookup or shared file supports; a timeout or abstention counts as no follow-up.

| Checks | OpenAI Decisions (threshold 0.8) | gpt-6-luna, 3 runs | gpt-6.1-sol, 3 runs |
| --- | --- | --- | --- |
| Previous defaults (`citations`, plus `source_use`), skipping attachments | 13 of 38 wrong | 17 of 114 wrong | 25 of 114 wrong |
| `research` | 2 of 38 wrong | 5 of 114 wrong | 2 of 114 wrong |

On OpenAI Decisions the previous checks missed most unresearched recommendations; on every measured backend they flagged replies whose searches supported them, because `citations` asks for citations in the answer rather than lookups.
Borderline replies, such as an opinion added to a supported answer, can move across the threshold with small wording changes, so treat their outcome as uncertain.
With a newer reply between the audited answer and the follow-up, a tool-free probe asked each model which claims it would check: with the previous wording, gpt-6-luna and gpt-6.1-sol listed claims from both answers 12 of 12 times; with the audited answer quoted, they listed only that answer's claims 12 of 12 times.
TypeSafe JEV was not measured because no key was available.

## Development

This is a directory plugin, matching other repositories in the MindRoom organization; no separate package installation is required.
With a sibling MindRoom checkout and its development dependencies installed:

```bash
uv run --project ../mindroom pytest tests
uv run --project ../mindroom mindroom plugins check .
uv run --project ../mindroom pre-commit run --all-files
```

On NixOS, run these inside the MindRoom `shell.nix` environment.
CI uses MindRoom's reusable plugin compatibility workflow pinned to `v2026.10.205`.
Tests exercise real hook contexts and mock the provider/network boundary; they do not establish live judgment accuracy or a calibrated threshold.
