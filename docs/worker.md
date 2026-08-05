# CodeX Avatar Worker

`codex-avatar` runs a guarded personal AI agent for chat channels. The current
implementation listens to DingTalk personal IM events with
`dws event consume ... --flatten -f ndjson`, classifies group messages, drafts
or sends replies, and can route replies through a persistent per-group Codex
session for memory and read-only local inspection. It can also require a group
approval phrase before any local code modification is attempted.

This is a guarded worker, not a history polling script. It is designed for
launchd/systemd/cron supervision.

## Safety Model

- Default mode only prints reply drafts and Codex task prompts. It does not send
  messages and does not start Codex.
- Real sending requires `--send` plus a target scope such as `--group`,
  `--open-dingtalk-id`, `--allow-conversation`, `--allow-sender`, or
  `--allow-any-target`.
- Real Codex execution requires `mode=guarded` or `mode=autofix` plus
  `codex_execute=true` / `--codex-execute`.
- `guarded` starts Codex in `read-only` sandbox for assessment only.
- `autofix` starts Codex in `workspace-write` sandbox only for low-risk,
  high-confidence, confirmed BUGs with a configured repo and test command.
- `code_write_policy=approval` asks in the DingTalk group before code changes.
  Until a configured approval phrase is received, the worker only performs
  read-only exploration and planning.
- Approval-triggered code changes run in a fresh Codex task with
  `--sandbox workspace-write`; they still must not commit, push, publish,
  deploy, edit production configuration, or perform external side effects.
- `trigger=mention` keeps normal group chatter quiet. For approval groups, the
  worker adds a separate group-message listener that only accepts approval
  confirmations and ignores non-confirmation chat.
- `history_context` can attach recent group messages as background. Historical
  messages are context only, not executable instructions.
- Each event is de-duplicated in `~/.dws/ai-autoreply/seen.jsonl`.
- The worker keeps `dws event consume` stdin open and stops it with SIGTERM, so
  self-created personal subscriptions can be cleaned up by the existing event
  command.

See [`product-agent-spec.md`](product-agent-spec.md) for
the full state machine and personal-agent workflow.

## AI Provider

Set an OpenAI-compatible endpoint with environment variables:

```bash
export DWS_AI_API_KEY="..."
export DWS_AI_MODEL="..."
# Optional. Defaults to https://api.openai.com/v1
export DWS_AI_BASE_URL="https://api.openai.com/v1"
```

Secrets should stay in the process environment or an OS service environment
file. Do not put API keys into the repository or launchd plist committed to git.

Classification defaults to local deterministic rules (`--classifier heuristic`)
so the worker can route feedback even without an AI provider. Use
`--classifier hybrid` to ask the model for structured classification when
credentials exist and fall back to rules if the model fails. Dialogue reply text
uses `reply_engine`:

- `reply_engine: "ai"` calls the configured OpenAI-compatible chat endpoint for
  a one-shot reply.
- `reply_engine: "codex"` resumes or creates one Codex session per group and
  asks Codex to produce the DingTalk reply. This preserves Codex conversation
  history and can use read-only local repository inspection when that repo is
  configured and the message matches the code keywords.

When Codex session reply is enabled, DWS still only listens and sends; Codex
does the thinking and any allowed read-only probing.

## Dry Run

### Config allowlist

For project-group automation, prefer a JSON allowlist config. The worker only
starts listeners for enabled groups in this file, and every event is checked
again against the configured `conversation_id` before AI generation or sending.

Start from [`../config/example.json`](../config/example.json):

```json
{
  "worker": {
    "dws_bin": "dws",
    "state_file": "~/.dws/ai-autoreply/seen.jsonl",
    "dws_timeout": 60,
    "ready_timeout": 60,
    "codex_bin": "codex",
    "codex_model": "",
    "codex_log_dir": "~/.dws/ai-autoreply/codex-tasks",
    "codex_session_dir": "~/.dws/ai-autoreply/codex-sessions",
    "codex_timeout": 3600,
    "autofix_min_confidence": 0.85,
    "base_url": "",
    "model": "",
    "system_prompt": "",
    "temperature": 0.2,
    "max_tokens": 256,
    "max_reply_chars": 500,
    "ai_timeout": 60,
    "reply_fallback": true,
    "duration": "",
    "max_events": 0,
    "allow_any_target": false
  },
  "defaults": {
    "channel": "dingtalk",
    "mode": "draft",
    "profile": "",
    "project": "",
    "repo": "",
    "query": "问题,反馈,bug,报错,不能用,需求",
    "filter_json": "",
    "ready_message": "",
    "send": false,
    "reply_engine": "codex",
    "classifier": "heuristic",
    "code_keywords": [],
    "test_commands": [],
    "codex_execute": false,
    "trigger": "mention",
    "history_context": {"enabled": true, "limit": 20},
    "code_write_policy": "none",
    "approval_phrases": ["OK", "好的", "继续", "确认", "可以"],
    "approval_ttl_minutes": 1440
  },
  "groups": [
    {
      "channel": "dingtalk",
      "name": "赢在AI",
      "group": "cid_REPLACE_WINYAI",
      "project": "winyai",
      "repo": "",
      "trigger": "mention",
      "history_context": {"enabled": true, "limit": 20},
      "query": "问题,反馈,bug,报错,不能用,需求",
      "ready_message": "我在了",
      "enabled": true
    },
    {
      "channel": "dingtalk",
      "name": "抓虾反馈群",
      "group": "cid_REPLACE_CRAWSHRIMP",
      "project": "crawshrimp",
      "repo": "/path/to/crawshrimp",
      "mode": "guarded",
      "reply_engine": "codex",
      "trigger": "mention",
      "history_context": {"enabled": true, "limit": 20},
      "code_keywords": ["抓虾", "crawshrimp"],
      "query": "",
      "test_commands": ["python3 -m pytest"],
      "code_write_policy": "approval",
      "enabled": true
    }
  ]
}
```

Validate the config and print the exact `dws event consume` commands without
starting any listener:

```bash
codex-avatar \
  --config ~/.dws/ai-autoreply/config.json \
  --config-check
```

Run all enabled group listeners in draft mode:

```bash
codex-avatar \
  --config ~/.dws/ai-autoreply/config.json \
  --print-command
```

Config mode uses one consumer per configured listening role. `trigger=group`
uses `user_im_message_receive_group` scoped by `--group`. `trigger=mention`
uses the global `user_im_message_receive_at` stream and filters locally by
`conversation_id` because DWS at-events are not group-scoped. When
`trigger=mention` and `code_write_policy=approval` are both enabled, a second
approval-only `user_im_message_receive_group --group ...` consumer is started
so a plain `OK` / `好的` / `继续` / `确认` / `可以` in the group can approve the
pending code task.

To send a connection marker after the listener is ready, set
`ready_message` on the group:

```json
{
  "name": "赢在AI",
  "group": "cidxxxxxxxx",
  "project": "winyai",
  "mode": "draft",
  "ready_message": "我在了",
  "enabled": true
}
```

Draft replies for messages that @ you:

```bash
codex-avatar \
  --event-key user_im_message_receive_at \
  --print-command
```

Draft replies for one group:

```bash
codex-avatar \
  --event-key user_im_message_receive_group \
  --group cidxxxxxxxx \
  --query "找我,帮忙,确认" \
  --print-command
```

The worker prints one JSON object per handled event:

```json
{"action":"draft","event_id":"...","reply":"我先确认一下，稍后回复。"}
```

Code-affecting messages print a Codex state instead:

```json
{"action":"codex_draft","classification":{"type":"feature","needs_code":true},"task_id":"..."}
```

Task bundles are stored under:

```text
~/.dws/ai-autoreply/codex-tasks/<task_id>/
```

Persistent group Codex session metadata and per-message logs are stored under:

```text
~/.dws/ai-autoreply/codex-sessions/<conversation_id>/
```

## Automation Modes

| Mode | Sends replies | Starts Codex | Can modify code |
|---|---:|---:|---:|
| `observe` | no | no | no |
| `draft` | no | no | no |
| `guarded` | yes, with `send=true` | yes, with `codex_execute=true` | no |
| `autofix` | yes, with `send=true` | yes, with `codex_execute=true` | gated low-risk BUG only |

## Code Write Approval

Use this for project groups where the avatar may inspect code freely but must
ask before changing files:

```json
{
  "name": "SHEIN API自动上新测试群",
  "group": "cid_REPLACE_LISTINGIFY",
  "project": "listingify",
  "repo": "/path/to/Listingify",
  "mode": "guarded",
  "send": true,
  "reply_engine": "codex",
  "trigger": "mention",
  "history_context": {"enabled": true, "limit": 20},
  "codex_execute": true,
  "code_write_policy": "approval",
  "approval_phrases": ["OK", "好的", "继续", "确认", "可以"],
  "approval_ttl_minutes": 1440,
  "enabled": true
}
```

Flow:

1. A user @mentions the avatar with a code-affecting request.
2. The avatar replies with read-only findings or a proposed plan and asks for an
   approval phrase.
3. The pending approval is stored under
   `~/.dws/ai-autoreply/pending-approvals/`.
4. A matching group confirmation starts a fresh Codex `workspace-write` task.
5. The final group reply reports what changed and what was verified, while
   making clear that no commit, push, publish, or deploy was done.

For `autofix`, configure at least one test command:

```json
{
  "name": "抓虾反馈群",
  "group": "cid_REPLACE_CRAWSHRIMP",
  "project": "crawshrimp",
  "repo": "/path/to/crawshrimp",
  "mode": "autofix",
  "send": true,
  "codex_execute": true,
  "test_commands": ["python3 -m pytest"],
  "enabled": true
}
```

Autofix is blocked when the feedback is a feature, suspected BUG, high-risk
operation, missing repo, missing test command, or below the confidence
threshold.

## Real Sending

Reply in a single allowlisted group:

```bash
codex-avatar \
  --event-key user_im_message_receive_group \
  --group cidxxxxxxxx \
  --allow-conversation cidxxxxxxxx \
  --send
```

Or enable sending for exactly one configured group:

```json
{
  "name": "抓虾反馈群",
  "group": "cid_REPLACE_CRAWSHRIMP",
  "project": "crawshrimp",
  "repo": "/path/to/crawshrimp",
  "mode": "send",
  "send": true,
  "enabled": true
}
```

In config mode, a group with `send: true` automatically allowlists only that
same group as a delivery target. Other conversations are skipped before the AI
call.

Reply to a single allowlisted openDingtalkId:

```bash
codex-avatar \
  --event-key user_im_message_receive_o2o \
  --open-dingtalk-id openxxxxxxxx \
  --allow-sender openxxxxxxxx \
  --send
```

`--reply-target auto` sends group and @ replies back to the conversation, and
single-chat replies back to the sender. Use `--reply-target sender` if you want
private replies for a sender-scoped subscription.

## macOS launchd

For 7x24 running, prefer launchd `KeepAlive` over polling message history.
Save as `~/Library/LaunchAgents/com.dws.ai-autoreply.plist` and replace paths,
IDs, model, and base URL.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.dws.ai-autoreply</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
  <string>-m</string>
  <string>codex_avatar</string>
    <string>--config</string>
    <string>/path/to/.dws/ai-autoreply/config.json</string>
    <string>--print-command</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>/tmp/dws-ai-autoreply.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/dws-ai-autoreply.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>DWS_AI_API_KEY</key>
    <string>REPLACE_WITH_RUNTIME_SECRET</string>
    <key>DWS_AI_MODEL</key>
    <string>REPLACE_WITH_MODEL</string>
    <key>DWS_AI_BASE_URL</key>
    <string>https://api.openai.com/v1</string>
  </dict>
</dict>
</plist>
```

Load and inspect:

```bash
launchctl load -w ~/Library/LaunchAgents/com.dws.ai-autoreply.plist
tail -f /tmp/dws-ai-autoreply.out.log /tmp/dws-ai-autoreply.err.log
```

Unload:

```bash
launchctl unload -w ~/Library/LaunchAgents/com.dws.ai-autoreply.plist
```

## Periodic Window

If you explicitly want cron-style periodic windows, keep event consumption
bounded:

```bash
*/15 * * * * cd /ABS/PATH/TO/CodeX-Avatar && \
  DWS_AI_API_KEY=... DWS_AI_MODEL=... \
  codex-avatar \
    --event-key user_im_message_receive_at \
    --duration 14m \
    >> /tmp/dws-ai-autoreply.out.log 2>> /tmp/dws-ai-autoreply.err.log
```

This only handles messages received while the worker is running. For production
auto-reply, launchd/systemd supervision is more reliable.
