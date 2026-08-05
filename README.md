# CodeX Avatar

CodeX Avatar 是一个通用的个人 AI 代理。它用于把个人常驻的聊天渠道接入 Codex 和 AI 回复能力，当前已支持钉钉渠道：通过 `dws event consume --flatten -f ndjson` 实时监听白名单群消息，先做通用消息分流，再生成回复草稿、真实回群、进入 Codex 只读评审，或在群内审批后交给 Codex 做本地代码修改。

项目定位是“个人代理”，不是固定业务角色。钉钉是当前第一个渠道，后续计划接入飞书和更多 IM / 协作渠道。

它不是轮询历史消息的脚本，也不是监听到消息就改代码的机器人。所有真实发送、Codex 执行和自动修复都需要显式开关、白名单、风险判断和测试命令。

## 能力

- 多群监听：一个启用群对应一个 `dws event consume` 子进程。
- 渠道配置：配置项包含 `channel`，当前支持 `dingtalk`，后续预留 `feishu` 和更多渠道。
- 触发控制：支持 `trigger=mention`，平时聊天不回复，只有被 @ 到才进入正常回复链路。
- 历史上下文：可从最近群聊记录读取背景信息，供 Codex session 理解上下文。
- 白名单准入：事件命中配置的 `conversation_id` 后才会进入 AI 或 Codex。
- 本地去重：按 `event_id`、`message_id` 或内容哈希记录到 `~/.dws/ai-autoreply/seen.jsonl`。
- 消息分类：支持 `question`、`support`、`feature`、`suspected_bug`、`bug`、`ops`、`noise`。
- 回复引擎：可选 OpenAI-compatible chat endpoint 或每群持久 Codex session。
- Codex 桥接：`guarded` 只读评审，`code_write_policy=approval` 时先在群里请求确认，再允许本地 `workspace-write` 修改。
- 审计输出：stdout 一行一个 JSON，记录 draft/sent/skipped/codex_* 等状态。

## 依赖

必需：

- Python 3.10+
- `dws` CLI：当前钉钉渠道用于 DingTalk 登录、事件消费、消息发送和历史消息读取
- `codex` CLI：当 `reply_engine=codex` 或需要代码评审/修改时使用

可选：

- OpenAI-compatible API：仅在 `reply_engine=ai` 或 `classifier=ai/hybrid` 时需要，环境变量为 `DWS_AI_API_KEY`、`DWS_AI_MODEL`、`DWS_AI_BASE_URL`
- macOS launchd：用于 7x24 后台运行

## 一键部署

最小安装会创建隔离 venv、安装本项目、查找并校验 `dws` / `codex`、创建本地配置、运行 `config-check`：

```bash
cd CodeX-Avatar
scripts/install.sh
```

安装并启动 macOS LaunchAgent：

```bash
scripts/install.sh \
  --config ~/.dws/ai-autoreply/config.json \
  --install-launchd \
  --start
```

如果 `dws` 或 `codex` 不在 PATH，可以显式传入：

```bash
scripts/install.sh \
  --dws-bin ~/.local/bin/dws \
  --codex-bin /Applications/ChatGPT.app/Contents/Resources/codex
```

脚本不会自动写入密钥，也不会在配置仍包含 `cid_REPLACE_*` 占位群 ID 时启动后台服务。

## 手动安装

当前 Python 包只依赖标准库，但运行时需要上面的外部 CLI。

```bash
cd CodeX-Avatar
python3 -m pip install -e .
```

确认 DWS 已登录：

```bash
dws auth status
```

需要 AI 直连回复或 AI 分类时，设置 OpenAI-compatible 环境变量：

```bash
export DWS_AI_API_KEY="..."
export DWS_AI_MODEL="..."
export DWS_AI_BASE_URL="https://api.openai.com/v1"
```

## 快速开始

复制配置样例到 git 外：

```bash
mkdir -p ~/.dws/ai-autoreply
cp config/example.json ~/.dws/ai-autoreply/config.json
```

把配置里的 `cid_REPLACE_*` 改成真实钉钉群 `openConversationId`，然后先做只读检查：

```bash
codex-avatar --config ~/.dws/ai-autoreply/config.json --config-check
```

以草稿模式启动所有启用群监听：

```bash
codex-avatar --config ~/.dws/ai-autoreply/config.json --print-command
```

监听一个群并只打印草稿：

```bash
codex-avatar \
  --event-key user_im_message_receive_group \
  --group cidxxxxxxxx \
  --query "问题,反馈,bug,报错,不能用,需求" \
  --print-command
```

真实发送必须显式 allowlist：

```bash
codex-avatar \
  --event-key user_im_message_receive_group \
  --group cidxxxxxxxx \
  --allow-conversation cidxxxxxxxx \
  --send
```

## 本地操作界面

启动本地 Web 控制台：

```bash
codex-avatar-ui --config ~/.dws/ai-autoreply/config.json --open
```

控制台默认运行在 `http://127.0.0.1:8787/`，可以编辑渠道群配置、保存配置、运行 `config-check`、启动 / 停止 worker，并查看实时日志。`启动监听` 会拒绝仍包含 `cid_REPLACE_*` 的启用群，避免用样例配置误启动。

本地调试时按 `Ctrl-C` 退出控制台。

## 配置面

配置文件分三层，网页操作台都会展示：

- `worker`：本地启动参数，包括 `dws_bin`、`state_file`、DWS/Codex 超时、`codex_bin`、`codex_model`、Codex 日志和 session 目录、AI base URL / 模型 / 温度 / token / 回复长度、`duration`、`max_events`、`allow_any_target`。
- `defaults`：所有群的默认值，包括 `channel`、`mode`、`trigger`、`profile`、`query`、`filter_json`、`reply_target`、`reply_engine`、`classifier`、历史上下文、发送开关、Codex 执行开关、写代码权限、审批确认词和测试命令。
- `groups`：每个群的覆盖值，包括群名、群 ID、项目、仓库、触发方式、回复与分类、历史背景、Codex 只读/写入审批、代码关键词、测试命令和就绪消息。

API key 不写入配置文件；AI 直连仍通过 `DWS_AI_API_KEY` 等环境变量提供。

## 模式

| 模式 | 发送回复 | 启动 Codex | 修改代码 |
|---|---:|---:|---:|
| `observe` | no | no | no |
| `draft` | no | no | no |
| `guarded` | yes, with `send=true` | yes, with `codex_execute=true` | no |
| `autofix` | yes, with `send=true` | yes, with `codex_execute=true` | gated low-risk BUG only |

推荐项目群使用 `trigger=mention` 和 `code_write_policy=approval`：平时群聊不会回复；需要改代码时先在群里发方案，等待 `OK / 好的 / 继续 / 确认 / 可以` 这类确认词。确认前只做只读探查，确认后只允许本地改代码，不会自动 commit、push、发布或部署。

`autofix` 是旧的自动修复模式，必须同时满足：白名单群、项目和 repo 已配置、`codex_execute=true`、分类为低风险高置信明确 BUG、repo 可访问、至少一条测试命令，并且消息不涉及权限、支付、生产配置、删除、发布、凭证等高风险事项。

## 文档

- [Worker 使用说明](docs/worker.md)
- [部署说明](docs/deployment.md)
- [个人代理状态机](docs/product-agent-spec.md)
- [配置样例](config/example.json)

## 测试

```bash
python3 -m py_compile src/codex_avatar/worker.py tests/test_worker.py
python3 -m py_compile src/codex_avatar/ui.py tests/test_ui.py
python3 tests/test_worker.py
python3 tests/test_ui.py
bash -n scripts/install.sh
codex-avatar --config config/example.json --config-check
```

## 许可证

Apache-2.0
