# Response Audit JEV

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

A [MindRoom](https://github.com/mindroom-ai/mindroom) hook plugin that uses [TypeSafe JEV](https://docs.typesafe.ai/api) to check completed answers and ask the responding agent to correct likely problems.

The agent answers first.
The plugin checks its citations and source use against the request and recorded tool activity, then posts one follow-up in the same thread, tagging the agent with specific correction guidance.
Checks are configurable, so the same mechanism can review units, formatting, or other requirements.

## Features

- Audits selected agents after their answers are delivered
- Includes citation and source-use checks, with support for custom questions
- Uses JEV by default or an existing MindRoom LLM model alias
- Checks recorded tool activity instead of treating the agent's lookup claims as evidence
- Combines findings into one same-thread follow-up that mentions the agent
- Skips hook-generated correction turns to prevent audit loops
- Prevents repeated audit attempts across replay and restart
- Uses existing public hooks and the shared judgment layer; no core changes or agent tools

## How It Works

1. An enabled agent receives a user request, and the plugin begins observing that turn.
2. Tool hooks record completed calls, including arguments, results, and success/failure status.
3. After the answer is delivered, JEV evaluates the request, answer, and tool evidence against each check.
4. If checks flag a clear issue, the plugin posts one message mentioning the agent with their configured correction guidance.
5. The agent handles that follow-up through MindRoom's normal permissions and dispatch flow.

The audit requests verification; it does not prove an answer wrong.
Missing or incomplete evidence stays quiet.
The judge receives message and tool contents, so enable it only where sending those contents to the configured provider is appropriate.
See [Evidence and Limits](docs/configuration.md#evidence-and-limits) for context bounds, privacy, and best-effort delivery behavior.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `begin-response-audit` | `message:enrich` | Start observing an eligible turn without changing its prompt |
| `record-audit-tool` | `tool:after_call` | Capture completed tool activity |
| `audit-response` | `message:after_response` | Check the delivered answer and request corrections |
| `discard-response-audit` | `message:cancelled` | Discard evidence for cancelled or failed replies |

## Requirements

MindRoom **v2026.9.232 or newer**, Python 3.13 or newer, and a `TYPESAFE_API_KEY` for JEV.
An LLM judge uses the configured model alias's normal credentials instead.

## Install

Vendor this plugin with the MindRoom CLI:

```bash
mindroom plugins install response-audit-jev-plugin
```

Update to the latest commit later with:

```bash
mindroom plugins update response-audit-jev-plugin
```

The command pins the exact installed commit in `.mindroom-plugin.lock.json` and strictly validates the plugin before activating it.
For a manual checkout, clone this repository into your `plugins/` directory.

## Setup

1. Set `TYPESAFE_API_KEY` in the MindRoom instance environment or config-adjacent `.env`.
2. Add the plugin to `config.yaml` and select the agents to audit:

   ```yaml
   plugins:
     - path: plugins/response-audit-jev-plugin
       settings:
         agents: [research]
         source_tools: [search_data]  # Replace with your actual tool function names
         judgment:
           provider: typesafe
           threshold: 0.9
           timeout_seconds: 1.5
   ```

3. Restart MindRoom or let its normal config reload apply the change.

No agent tools are required.
`agents` defaults to `[]`, so installation alone audits nobody.
Settings follow selected agents across authorized rooms; teams are excluded.
`source_tools` names the actual search, database, or document-retrieval functions observed by tool hooks.
Without it, only the citation check runs by default.
The probability threshold is a starting point, not a calibrated accuracy guarantee.

See [Configuration and Custom Checks](docs/configuration.md) for all settings, LLM configuration, custom questions, and development commands.
