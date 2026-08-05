# CodeX Avatar 部署说明

## 依赖

必需：

- Python 3.10+
- `dws` CLI，并已完成 `dws auth status`
- `codex` CLI；macOS ChatGPT App 内置路径通常是 `/Applications/ChatGPT.app/Contents/Resources/codex`

可选：

- OpenAI-compatible API 环境变量：`DWS_AI_API_KEY`、`DWS_AI_MODEL`、`DWS_AI_BASE_URL`
- macOS launchd，用于后台常驻

## 一键安装

```bash
scripts/install.sh
```

脚本会：

- 创建 `~/.dws/codex-avatar/venv`
- 在 venv 里安装当前项目
- 查找 `dws` 和 `codex`
- 首次运行时复制 `config/example.json` 到 `~/.dws/ai-autoreply/config.json`
- 运行 `codex-avatar --config-check`

如果 CLI 不在 PATH：

```bash
scripts/install.sh \
  --dws-bin ~/.local/bin/dws \
  --codex-bin /Applications/ChatGPT.app/Contents/Resources/codex
```

## 后台运行

macOS 上可以安装并启动 LaunchAgent：

```bash
scripts/install.sh \
  --config ~/.dws/ai-autoreply/config.json \
  --install-launchd \
  --start
```

如果配置里仍有 `cid_REPLACE_*`，脚本会拒绝启动后台进程。先把配置里的群 ID、项目 repo 路径和是否发送回复改好，再重新执行。

## 推荐项目群配置

```json
{
  "name": "SHEIN API自动上新测试群",
  "group": "cid_REPLACE_LISTINGIFY",
  "project": "listingify",
  "repo": "/Users/xingyicheng/Documents/Listingify",
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

这个组合表示：

- 平时群聊不回复，只有被 @ 到才处理。
- 回复前会读取最近 20 条历史消息作为背景。
- 需要改代码时先在群里请求确认。
- 群里直接回复 `OK`、`好的`、`继续`、`确认` 或 `可以` 后，才进入本地代码修改。
- 修改阶段不会自动提交、推送、发布、部署或改生产配置。

## 常用检查

```bash
codex-avatar --config ~/.dws/ai-autoreply/config.json --config-check
dws event status --event user_im_message_receive_at --format json
dws event status --event user_im_message_receive_group --format json
```
