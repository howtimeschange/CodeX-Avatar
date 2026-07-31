#!/usr/bin/env python3
"""
Consume DingTalk personal IM events, draft replies with an OpenAI-compatible
chat endpoint, and optionally send the replies through `dws chat message send`.

The worker is intentionally conservative:

- dry-run by default; pass --send to deliver messages
- real sending requires an explicit target scope or --allow-any-target
- events are de-duplicated in a local state file
- event consumption uses `dws event consume ... --flatten -f ndjson`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


RECEIVE_EVENTS = {
    "user_im_message_receive_at",
    "user_im_message_receive_o2o",
    "user_im_message_receive_group",
    "user_im_message_receive_user",
}

DEFAULT_SYSTEM_PROMPT = """你是用户的钉钉 AI 代回复助手。
请根据收到的消息生成简短、礼貌、清楚的中文回复。
不要假装已经完成未确认的工作；不确定时回复“我先确认一下，稍后回复”。
不要索取或复述密码、密钥、验证码、身份证、银行卡、付款等敏感信息。
不要代表用户做无法撤回或高风险承诺。"""

CLASSIFIER_SYSTEM_PROMPT = """你是一个产品经理型反馈分流器。
请把钉钉群消息分类为 question、support、feature、suspected_bug、bug、ops 或 noise。
必须输出 JSON 对象，字段包括 type、confidence、needs_code、risk、reply_draft。
risk 只能是 low、medium、high。不要输出 Markdown。"""

FEEDBACK_TYPES = {
    "question",
    "support",
    "feature",
    "suspected_bug",
    "bug",
    "ops",
    "noise",
}
RISK_LEVELS = {"low", "medium", "high"}
AUTOMATION_MODES = {"observe", "draft", "guarded", "autofix", "send"}

HIGH_RISK_RE = re.compile(
    r"支付|付款|扣款|退款|发票|合同|权限|授权|密码|密钥|token|验证码|身份证|银行卡|"
    r"删除|清空|撤回|生产|线上|发布|上线|数据库|迁移|回滚|配置中心|域名|证书|账单",
    re.IGNORECASE,
)
BUG_RE = re.compile(
    r"bug|报错|错误|异常|崩溃|闪退|白屏|失败|不生效|不能用|打不开|卡住|一直加载|"
    r"不对|不准|少了|丢失|无法|fail|error|exception",
    re.IGNORECASE,
)
SUSPECTED_BUG_RE = re.compile(
    r"好像|疑似|是不是|感觉|偶现|有时候|不稳定|怪|奇怪|不太对|复现不了",
    re.IGNORECASE,
)
FEATURE_RE = re.compile(
    r"需求|建议|希望|能不能|可不可以|加个|增加|新增|优化|支持|想要|最好|"
    r"feature|enhancement",
    re.IGNORECASE,
)
SUPPORT_RE = re.compile(r"怎么|如何|在哪|哪里|教程|使用|配置|操作|为什么|能帮|帮忙", re.IGNORECASE)
OPS_RE = re.compile(r"发布|上线|部署|权限|账号|配置|数据|导入|导出|同步|审批|流程", re.IGNORECASE)
NOISE_RE = re.compile(r"^(好的|收到|ok|OK|嗯|嗯嗯|谢谢|辛苦了|1|。|\.|👍|👌|\s*)$")


@dataclass(frozen=True)
class ReplyTarget:
    flag: str
    value: str


@dataclass(frozen=True)
class GroupConfig:
    name: str
    group_id: str
    project: str = ""
    repo: str = ""
    code_keywords: tuple[str, ...] = ()
    reply_engine: str = ""
    classifier: str = ""
    profile: str = ""
    query: str = ""
    filter_json: str = ""
    reply_target: str = "auto"
    mode: str = "draft"
    send: bool = False
    ready_message: str = ""
    test_commands: tuple[str, ...] = ()
    codex_execute: bool = False


@dataclass
class ManagedConsumer:
    label: str
    args: argparse.Namespace
    process: subprocess.Popen[str]
    ready: threading.Event
    stdout_thread: Optional[threading.Thread] = None


@dataclass(frozen=True)
class ClassificationResult:
    project: str
    type: str
    confidence: float
    needs_code: bool
    risk: str
    reply_draft: str
    codex_task: dict[str, Any]
    source: str = "heuristic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "type": self.type,
            "confidence": self.confidence,
            "needs_code": self.needs_code,
            "risk": self.risk,
            "reply_draft": self.reply_draft,
            "codex_task": self.codex_task,
            "source": self.source,
        }


@dataclass(frozen=True)
class CodexBridgeResult:
    task_id: str
    status: str
    task_dir: str
    delivery_message: str
    reason: str = ""
    command: tuple[str, ...] = ()
    executed: bool = False
    returncode: Optional[int] = None
    last_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "task_id": self.task_id,
            "status": self.status,
            "task_dir": self.task_dir,
            "delivery_message": self.delivery_message,
            "reason": self.reason,
            "executed": self.executed,
        }
        if self.command:
            payload["command"] = list(self.command)
        if self.returncode is not None:
            payload["returncode"] = self.returncode
        if self.last_message:
            payload["last_message"] = self.last_message
        return payload


def parse_csv_values(values: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for raw in values:
        for part in raw.split(","):
            item = part.strip()
            if item:
                out.add(item)
    return out


def default_state_file() -> Path:
    return Path.home() / ".dws" / "ai-autoreply" / "seen.jsonl"


def default_config_file() -> Path:
    return Path.home() / ".dws" / "ai-autoreply" / "config.json"


def clone_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(**vars(args))


def config_string(item: dict[str, Any], *names: str) -> str:
    for name in names:
        value = item.get(name)
        if value is None:
            continue
        value = str(value).strip()
        if value:
            return value
    return ""


def config_bool(item: dict[str, Any], name: str, default: bool = False) -> bool:
    value = item.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def config_string_list(item: dict[str, Any], *names: str) -> tuple[str, ...]:
    for name in names:
        value = item.get(name)
        if value is None:
            continue
        if isinstance(value, list):
            out = tuple(str(part).strip() for part in value if str(part).strip())
            if out:
                return out
        if isinstance(value, str):
            value = value.strip()
            if value:
                return (value,)
    return ()


def normalize_mode(mode: str) -> str:
    mode = (mode or "draft").strip().lower()
    if mode not in AUTOMATION_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(AUTOMATION_MODES))}")
    return mode


def effective_mode(args: argparse.Namespace) -> str:
    mode = normalize_mode(str(getattr(args, "mode", "") or "draft"))
    if mode == "send":
        return "guarded"
    return mode


def load_agent_config(path: Path) -> tuple[dict[str, Any], list[GroupConfig]]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("config root must be an object")
    defaults = raw.get("defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("config.defaults must be an object")
    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("config.groups must be a non-empty array")

    out: list[GroupConfig] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(groups):
        if not isinstance(item, dict):
            raise ValueError(f"config.groups[{index}] must be an object")
        if not config_bool(item, "enabled", True):
            continue
        group_id = config_string(item, "group", "group_id", "id", "openConversationId", "open_conversation_id")
        if not group_id:
            raise ValueError(f"config.groups[{index}] is missing group/openConversationId")
        if group_id in seen_ids:
            raise ValueError(f"duplicate group id in config: {group_id}")
        seen_ids.add(group_id)
        mode = normalize_mode(config_string(item, "mode") or config_string(defaults, "mode") or "draft")
        send = config_bool(item, "send", config_bool(defaults, "send", False))
        if mode == "send":
            send = True
        test_commands = (
            config_string_list(item, "test_commands", "testCommands", "test_command", "testCommand")
            or config_string_list(defaults, "test_commands", "testCommands", "test_command", "testCommand")
        )
        code_keywords = (
            config_string_list(item, "code_keywords", "codeKeywords", "repo_keywords", "repoKeywords")
            or config_string_list(defaults, "code_keywords", "codeKeywords", "repo_keywords", "repoKeywords")
        )
        codex_execute = config_bool(item, "codex_execute", config_bool(defaults, "codex_execute", False))
        reply_engine = (
            config_string(item, "reply_engine", "replyEngine")
            or config_string(defaults, "reply_engine", "replyEngine")
        )
        if reply_engine and reply_engine not in {"ai", "codex"}:
            raise ValueError("reply_engine must be one of: ai, codex")
        classifier = config_string(item, "classifier") or config_string(defaults, "classifier")
        if classifier and classifier not in {"heuristic", "hybrid", "ai"}:
            raise ValueError("classifier must be one of: heuristic, hybrid, ai")
        out.append(GroupConfig(
            name=config_string(item, "name") or group_id,
            group_id=group_id,
            project=config_string(item, "project") or config_string(defaults, "project"),
            repo=config_string(item, "repo", "cwd") or config_string(defaults, "repo", "cwd"),
            code_keywords=code_keywords,
            reply_engine=reply_engine,
            classifier=classifier,
            profile=config_string(item, "profile") or config_string(defaults, "profile"),
            query=config_string(item, "query") or config_string(defaults, "query"),
            filter_json=config_string(item, "filter_json", "filterJson") or config_string(defaults, "filter_json", "filterJson"),
            reply_target=config_string(item, "reply_target", "replyTarget") or config_string(defaults, "reply_target", "replyTarget") or "auto",
            mode=mode,
            send=send,
            ready_message=config_string(item, "ready_message", "readyMessage") or config_string(defaults, "ready_message", "readyMessage"),
            test_commands=test_commands,
            codex_execute=codex_execute,
        ))
    if not out:
        raise ValueError("config has no enabled groups")
    return defaults, out


def args_for_group(base: argparse.Namespace, defaults: dict[str, Any], group: GroupConfig) -> argparse.Namespace:
    args = clone_namespace(base)
    args.event_key = "user_im_message_receive_group"
    args.group = group.group_id
    args.user = ""
    args.open_dingtalk_id = ""
    args.profile = group.profile or args.profile
    args.query = group.query or args.query
    args.filter_json = group.filter_json or args.filter_json
    args.reply_target = group.reply_target or args.reply_target
    args.send = group.send or args.send
    args.allow_any_target = bool(defaults.get("allow_any_target", False)) or args.allow_any_target
    args.allow_conversation = set(args.allow_conversation)
    args.allow_conversation.add(group.group_id)
    args.admit_conversation = set(args.admit_conversation)
    args.admit_conversation.add(group.group_id)
    args.group_name = group.name
    args.project = group.project
    args.repo = group.repo
    args.code_keyword = tuple(group.code_keywords or tuple(args.code_keyword))
    args.reply_engine = group.reply_engine or args.reply_engine
    args.classifier = group.classifier or args.classifier
    args.mode = group.mode
    args.ready_message = group.ready_message or args.ready_message
    args.test_command = tuple(group.test_commands or tuple(args.test_command))
    args.codex_execute = bool(group.codex_execute or args.codex_execute)
    return args


def load_seen(path: Path) -> set[str]:
    seen: set[str] = set()
    if not path.exists():
        return seen
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(item.get("key", "")).strip()
            if key:
                seen.add(key)
    return seen


def append_seen(path: Path, key: str, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "key": key,
            "status": status,
            "ts": int(time.time()),
        }, ensure_ascii=False) + "\n")


def event_key_for_dedupe(event: dict[str, Any]) -> str:
    for field in ("event_id", "message_id"):
        value = str(event.get(field, "")).strip()
        if value:
            return value
    raw = json.dumps(event, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_consume_command(args: argparse.Namespace) -> list[str]:
    cmd = [args.dws_bin]
    if args.profile:
        cmd.extend(["--profile", args.profile])
    cmd.extend(["event", "consume", args.event_key, "--flatten", "-f", "ndjson"])
    if args.group:
        cmd.extend(["--group", args.group])
    if args.user:
        cmd.extend(["--user", args.user])
    if args.open_dingtalk_id:
        cmd.extend(["--open-dingtalk-id", args.open_dingtalk_id])
    if args.query:
        cmd.extend(["--query", args.query])
    if args.filter_json:
        cmd.extend(["--filter-json", args.filter_json])
    if args.duration:
        cmd.extend(["--duration", args.duration])
    if args.max_events:
        cmd.extend(["--max-events", str(args.max_events)])
    return cmd


def resolve_reply_target(event: dict[str, Any], configured_event: str, mode: str) -> ReplyTarget:
    event_type = str(event.get("type") or configured_event)
    if mode == "auto":
        if event_type in {"user_im_message_receive_at", "user_im_message_receive_group"}:
            mode = "conversation"
        else:
            mode = "sender"

    if mode == "conversation":
        value = str(event.get("conversation_id", "")).strip()
        if not value:
            raise ValueError("event has no conversation_id for conversation reply")
        return ReplyTarget("--group", value)

    value = str(event.get("sender_open_dingtalk_id", "")).strip()
    if not value:
        raise ValueError("event has no sender_open_dingtalk_id for sender reply")
    return ReplyTarget("--open-dingtalk-id", value)


def target_allowed(target: ReplyTarget, args: argparse.Namespace) -> bool:
    if args.allow_any_target:
        return True
    if target.flag == "--group":
        return target.value in args.allow_conversation or target.value == args.group
    if target.flag == "--open-dingtalk-id":
        return (
            target.value in args.allow_sender
            or target.value == args.open_dingtalk_id
        )
    return False


def has_send_scope(args: argparse.Namespace) -> bool:
    return bool(
        args.allow_any_target
        or args.allow_conversation
        or args.allow_sender
        or args.group
        or args.open_dingtalk_id
    )


def context_fields(args: argparse.Namespace) -> dict[str, str]:
    fields = {
        "project": str(getattr(args, "project", "") or "").strip(),
        "group_name": str(getattr(args, "group_name", "") or "").strip(),
        "group": str(getattr(args, "group", "") or "").strip(),
        "mode": str(getattr(args, "mode", "") or "").strip(),
    }
    return {key: value for key, value in fields.items() if value}


def build_prompt_payload(event: dict[str, Any], args: argparse.Namespace) -> str:
    payload = {
        "sender": event.get("sender", ""),
        "content": event.get("content", ""),
        "conversation_id": event.get("conversation_id", ""),
        "message_id": event.get("message_id", ""),
        "event_time": event.get("event_time", ""),
    }
    context = context_fields(args)
    if context:
        payload["agent_context"] = context
    return json.dumps(payload, ensure_ascii=False, indent=2)


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def ai_credentials(args: argparse.Namespace) -> tuple[str, str, str]:
    api_key = args.api_key or os.getenv("DWS_AI_API_KEY") or os.getenv("OPENAI_API_KEY")
    model = args.model or os.getenv("DWS_AI_MODEL") or os.getenv("OPENAI_MODEL")
    base_url = (
        args.base_url
        or os.getenv("DWS_AI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    )
    return api_key, model, base_url


def has_ai_credentials(args: argparse.Namespace) -> bool:
    api_key, model, _base_url = ai_credentials(args)
    return bool(api_key and model)


def call_chat_completion(
    messages: list[dict[str, str]],
    args: argparse.Namespace,
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> dict[str, Any]:
    api_key, model, base_url = ai_credentials(args)
    if not api_key:
        raise RuntimeError("missing AI API key: set DWS_AI_API_KEY or pass --api-key")
    if not model:
        raise RuntimeError("missing AI model: set DWS_AI_MODEL or pass --model")

    body = {
        "model": model,
        "messages": messages,
        "temperature": args.temperature if temperature is None else temperature,
        "max_tokens": args.max_tokens if max_tokens is None else max_tokens,
    }
    request = urllib.request.Request(
        chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.ai_timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI HTTP {e.code}: {detail[:500]}") from e
    return json.loads(raw)


def extract_reply(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("AI response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("AI response choice is not an object")
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    text = first.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    raise ValueError("AI response has no message content")


def clip_reply(reply: str, max_chars: int) -> str:
    reply = reply.strip()
    if max_chars <= 0 or len(reply) <= max_chars:
        return reply
    return reply[:max_chars].rstrip() + "..."


def generate_reply(event: dict[str, Any], args: argparse.Namespace) -> str:
    data = call_chat_completion(
        [
            {"role": "system", "content": args.system_prompt},
            {
                "role": "user",
                "content": "请只输出要发送的回复正文。\n收到的钉钉消息 JSON:\n" + build_prompt_payload(event, args),
            },
        ],
        args,
    )
    return clip_reply(extract_reply(data), args.max_reply_chars)


def extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("classifier JSON must be an object")
    return data


def normalize_classification_payload(
    payload: dict[str, Any],
    event: dict[str, Any],
    args: argparse.Namespace,
    *,
    source: str,
) -> ClassificationResult:
    project = str(payload.get("project") or getattr(args, "project", "") or "").strip()
    feedback_type = str(payload.get("type") or "support").strip().lower()
    type_aliases = {
        "咨询": "question",
        "操作问题": "support",
        "需求": "feature",
        "疑似bug": "suspected_bug",
        "疑似_bug": "suspected_bug",
        "明确bug": "bug",
        "明确_bug": "bug",
        "bug_like": "suspected_bug",
    }
    feedback_type = type_aliases.get(feedback_type, feedback_type)
    if feedback_type not in FEEDBACK_TYPES:
        feedback_type = "support"

    try:
        confidence = float(payload.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    risk = str(payload.get("risk") or "medium").strip().lower()
    if risk not in RISK_LEVELS:
        risk = "medium"

    reply_draft = str(payload.get("reply_draft") or payload.get("reply") or "").strip()
    if not reply_draft:
        reply_draft = default_reply_for_type(feedback_type)

    needs_code = bool(payload.get("needs_code", feedback_type in {"bug", "suspected_bug", "feature"}))
    if feedback_type in {"question", "support", "ops", "noise"}:
        needs_code = bool(payload.get("needs_code", False))
    repo = str(getattr(args, "repo", "") or "").strip()
    if needs_code and repo and not code_scope_allowed(event, args):
        needs_code = False
    codex_task = payload.get("codex_task")
    if not isinstance(codex_task, dict):
        codex_task = {}
    if needs_code:
        codex_task = build_codex_task_payload(event, args, feedback_type, codex_task)
    elif repo:
        codex_task = {"repo": repo, "evidence": evidence_refs(event)}
    return ClassificationResult(
        project=project,
        type=feedback_type,
        confidence=confidence,
        needs_code=needs_code,
        risk=risk,
        reply_draft=clip_reply(reply_draft, args.max_reply_chars),
        codex_task=codex_task,
        source=source,
    )


def evidence_refs(event: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for prefix, field in (
        ("ding_event", "event_id"),
        ("ding_message", "message_id"),
        ("ding_conversation", "conversation_id"),
    ):
        value = str(event.get(field, "")).strip()
        if value:
            refs.append(f"{prefix}:{value}")
    return refs


def code_scope_allowed(event: dict[str, Any], args: argparse.Namespace) -> bool:
    keywords = tuple(
        str(item).strip().lower()
        for item in (getattr(args, "code_keyword", ()) or ())
        if str(item).strip()
    )
    if not keywords:
        return True
    content = str(event.get("content", "") or "").lower()
    return any(keyword in content for keyword in keywords)


def build_codex_task_payload(
    event: dict[str, Any],
    args: argparse.Namespace,
    feedback_type: str,
    existing: dict[str, Any],
) -> dict[str, Any]:
    repo = str(existing.get("repo") or getattr(args, "repo", "") or "").strip()
    goal = str(existing.get("goal") or "").strip()
    if not goal:
        if feedback_type == "feature":
            goal = "评审用户反馈的新需求，评估改动面、风险和验收标准。"
        elif feedback_type == "suspected_bug":
            goal = "复现并判断用户反馈是否为 BUG；信息不足时给出追问。"
        else:
            goal = "复现并修复用户反馈的明确 BUG，完成测试并给出交付说明。"
    evidence = list(existing.get("evidence") or [])
    for ref in evidence_refs(event):
        if ref not in evidence:
            evidence.append(ref)
    return {
        "repo": repo,
        "goal": goal,
        "evidence": evidence,
        "acceptance": list(existing.get("acceptance") or default_acceptance(feedback_type)),
        "test_commands": list(getattr(args, "test_command", ()) or ()),
    }


def default_acceptance(feedback_type: str) -> list[str]:
    if feedback_type == "feature":
        return ["给出需求评审结论、改动面、风险、验收标准和是否需要人工确认。"]
    if feedback_type == "suspected_bug":
        return ["先复现或定位证据；无法复现时明确需要补充的信息。"]
    return ["确认根因", "实现低风险修复", "运行配置的测试命令", "输出交付说明和待发布状态"]


def default_reply_for_type(feedback_type: str) -> str:
    if feedback_type == "question":
        return "我看到了，这个可以先通过对话确认，我马上回复你。"
    if feedback_type == "support":
        return "收到，我先帮你判断操作路径，能直接解决的话我就在群里回复。"
    if feedback_type == "feature":
        return "收到，这是一个需求反馈，我会先评估改动面和验收标准。"
    if feedback_type == "suspected_bug":
        return "收到，这个像是问题反馈，我先判断是否能复现；信息不够会继续追问。"
    if feedback_type == "bug":
        return "收到，按 BUG 反馈进入排查，我会先评估改动面和验证路径。"
    if feedback_type == "ops":
        return "收到，这类涉及配置或发布操作，我会先做风险判断，需要确认再继续。"
    return ""


def heuristic_classify(event: dict[str, Any], args: argparse.Namespace) -> ClassificationResult:
    content = str(event.get("content", "") or "").strip()
    project = str(getattr(args, "project", "") or "").strip()
    repo = str(getattr(args, "repo", "") or "").strip()
    if NOISE_RE.match(content):
        payload = {
            "project": project,
            "type": "noise",
            "confidence": 0.9,
            "needs_code": False,
            "risk": "low",
            "reply_draft": "",
        }
        return normalize_classification_payload(payload, event, args, source="heuristic")

    high_risk = bool(HIGH_RISK_RE.search(content))
    has_bug = bool(BUG_RE.search(content))
    has_feature = bool(FEATURE_RE.search(content))
    has_support = bool(SUPPORT_RE.search(content))
    has_ops = bool(OPS_RE.search(content))
    suspected = bool(SUSPECTED_BUG_RE.search(content))

    if has_bug:
        feedback_type = "suspected_bug" if suspected else "bug"
        confidence = 0.76 if suspected else 0.88
    elif has_feature:
        feedback_type = "feature"
        confidence = 0.8
    elif has_ops and high_risk:
        feedback_type = "ops"
        confidence = 0.78
    elif has_support:
        feedback_type = "support" if re.search(r"操作|配置|使用|怎么|如何|在哪|哪里", content) else "question"
        confidence = 0.74
    else:
        feedback_type = "support"
        confidence = 0.55

    risk = "high" if high_risk else "medium"
    if feedback_type in {"question", "support", "noise"} and not high_risk:
        risk = "low"
    if feedback_type == "bug" and not high_risk:
        risk = "low"
    if feedback_type in {"feature", "suspected_bug"} and not high_risk:
        risk = "medium"
    needs_code = feedback_type in {"bug", "suspected_bug", "feature"} and bool(repo)
    payload = {
        "project": project,
        "type": feedback_type,
        "confidence": confidence,
        "needs_code": needs_code,
        "risk": risk,
        "reply_draft": default_reply_for_type(feedback_type),
    }
    return normalize_classification_payload(payload, event, args, source="heuristic")


def generate_ai_classification(event: dict[str, Any], args: argparse.Namespace) -> ClassificationResult:
    data = call_chat_completion(
        [
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "请分类这条钉钉消息，输出 JSON。\n上下文:\n" + build_prompt_payload(event, args),
            },
        ],
        args,
        max_tokens=300,
        temperature=0,
    )
    payload = extract_json_object(extract_reply(data))
    return normalize_classification_payload(payload, event, args, source="ai")


def classify_feedback(event: dict[str, Any], args: argparse.Namespace) -> ClassificationResult:
    classifier = str(getattr(args, "classifier", "heuristic") or "heuristic")
    if classifier == "heuristic":
        return heuristic_classify(event, args)
    if classifier == "ai":
        return generate_ai_classification(event, args)
    if classifier == "hybrid" and has_ai_credentials(args):
        try:
            return generate_ai_classification(event, args)
        except Exception as e:
            print(f"WARN: AI classifier failed, falling back to heuristic: {e}", file=sys.stderr)
    return heuristic_classify(event, args)


def send_uuid(event: dict[str, Any], target: ReplyTarget, purpose: str = "reply") -> str:
    basis = "|".join([
        purpose,
        str(event.get("event_id", "")),
        str(event.get("message_id", "")),
        target.flag,
        target.value,
    ])
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return "ai-autoreply-" + digest


def build_send_command(
    dws_bin: str,
    profile: str,
    target: ReplyTarget,
    reply: str,
    uuid: str,
) -> list[str]:
    cmd = [dws_bin]
    if profile:
        cmd.extend(["--profile", profile])
    cmd.extend([
        "chat", "message", "send",
        target.flag, target.value,
        "--text", reply,
        "--uuid", uuid,
        "--format", "json",
    ])
    return cmd


def ready_uuid(target: ReplyTarget, message: str) -> str:
    today = time.strftime("%Y%m%d", time.localtime())
    basis = "|".join(["ready", today, target.flag, target.value, message])
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return "ai-autoreply-ready-" + digest


def build_ready_message_command(args: argparse.Namespace) -> list[str]:
    if args.group:
        target = ReplyTarget("--group", args.group)
    elif args.open_dingtalk_id:
        target = ReplyTarget("--open-dingtalk-id", args.open_dingtalk_id)
    else:
        raise ValueError("ready message requires --group or --open-dingtalk-id")
    return build_send_command(
        args.dws_bin,
        args.profile,
        target,
        args.ready_message,
        ready_uuid(target, args.ready_message),
    )


def duplicate_uuid_payload(stderr: str) -> dict[str, Any]:
    if "Request is repeated with uuid" not in stderr:
        return {}
    match = re.search(r"uuid '([^']+)'", stderr)
    payload = {"duplicate_uuid": True}
    if match:
        payload["uuid"] = match.group(1)
    return payload


def send_ready_message(args: argparse.Namespace) -> dict[str, Any]:
    if not str(args.ready_message).strip():
        return {}
    cmd = build_ready_message_command(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=args.dws_timeout,
        check=False,
    )
    if result.returncode != 0:
        duplicate = duplicate_uuid_payload(result.stderr)
        if duplicate:
            log_json({
                "action": "ready_message_sent",
                "target": args.group or args.open_dingtalk_id,
                "message": args.ready_message,
                "idempotent": True,
                **duplicate,
                **context_fields(args),
            })
            return duplicate
        raise RuntimeError(result.stderr.strip() or f"dws exited {result.returncode}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": result.stdout.strip()}
    log_json({
        "action": "ready_message_sent",
        "target": args.group or args.open_dingtalk_id,
        "message": args.ready_message,
        **context_fields(args),
    })
    return payload


def send_reply(
    event: dict[str, Any],
    target: ReplyTarget,
    reply: str,
    args: argparse.Namespace,
    *,
    purpose: str = "reply",
) -> dict[str, Any]:
    cmd = build_send_command(args.dws_bin, args.profile, target, reply, send_uuid(event, target, purpose))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=args.dws_timeout,
        check=False,
    )
    if result.returncode != 0:
        duplicate = duplicate_uuid_payload(result.stderr)
        if duplicate:
            return duplicate
        raise RuntimeError(result.stderr.strip() or f"dws exited {result.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"stdout": result.stdout.strip()}


def task_id_for_event(event: dict[str, Any], classification: ClassificationResult) -> str:
    basis = "|".join([
        str(event.get("event_id", "")),
        str(event.get("message_id", "")),
        str(event.get("conversation_id", "")),
        classification.project,
        classification.type,
    ])
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]
    return "dws-codex-" + digest


def codex_task_dir(args: argparse.Namespace, task_id: str) -> Path:
    base = Path(getattr(args, "codex_log_dir", "") or (Path.home() / ".dws" / "ai-autoreply" / "codex-tasks"))
    return base.expanduser() / task_id


def codex_session_base_dir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "codex_session_dir", "") or (Path.home() / ".dws" / "ai-autoreply" / "codex-sessions")).expanduser()


def codex_session_key(event: dict[str, Any], args: argparse.Namespace) -> str:
    raw = str(event.get("conversation_id") or getattr(args, "group", "") or getattr(args, "group_name", "") or "default")
    safe = re.sub(r"[^A-Za-z0-9_.=-]+", "_", raw).strip("_")
    return safe or "default"


def codex_session_dir_for_event(event: dict[str, Any], args: argparse.Namespace) -> Path:
    return codex_session_base_dir(args) / codex_session_key(event, args)


def codex_session_state_path(event: dict[str, Any], args: argparse.Namespace) -> Path:
    return codex_session_dir_for_event(event, args) / "session.json"


def load_codex_session_id(event: dict[str, Any], args: argparse.Namespace) -> str:
    path = codex_session_state_path(event, args)
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(data.get("thread_id", "")).strip()


def save_codex_session_id(event: dict[str, Any], args: argparse.Namespace, thread_id: str) -> None:
    if not thread_id:
        return
    path = codex_session_state_path(event, args)
    payload = {
        "thread_id": thread_id,
        "conversation_id": event.get("conversation_id", ""),
        "group_name": getattr(args, "group_name", ""),
        "project": getattr(args, "project", ""),
        "repo": getattr(args, "repo", ""),
        "updated_at": int(time.time()),
    }
    write_json_file(path, payload)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def repo_available(repo: str) -> bool:
    return bool(repo) and Path(repo).expanduser().is_dir()


def test_commands(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in (getattr(args, "test_command", ()) or ()) if str(item).strip())


def is_autofix_eligible(classification: ClassificationResult, args: argparse.Namespace) -> tuple[bool, str]:
    repo = str(classification.codex_task.get("repo") or getattr(args, "repo", "") or "").strip()
    if effective_mode(args) != "autofix":
        return False, "mode_not_autofix"
    if not bool(getattr(args, "codex_execute", False)):
        return False, "codex_execute_disabled"
    if classification.type != "bug":
        return False, "not_confirmed_bug"
    if classification.confidence < float(getattr(args, "autofix_min_confidence", 0.85)):
        return False, "classification_confidence_too_low"
    if classification.risk != "low":
        return False, "risk_not_low"
    if not repo_available(repo):
        return False, "repo_missing_or_unavailable"
    if not test_commands(args):
        return False, "test_commands_missing"
    return True, ""


def should_run_codex_review(classification: ClassificationResult, args: argparse.Namespace) -> tuple[bool, str]:
    repo = str(classification.codex_task.get("repo") or getattr(args, "repo", "") or "").strip()
    if not bool(getattr(args, "codex_execute", False)):
        return False, "codex_execute_disabled"
    if effective_mode(args) not in {"guarded", "autofix"}:
        return False, "mode_does_not_execute_codex"
    if not repo_available(repo):
        return False, "repo_missing_or_unavailable"
    return True, ""


def build_codex_prompt(event: dict[str, Any], classification: ClassificationResult, args: argparse.Namespace) -> str:
    mode = effective_mode(args)
    task = classification.codex_task
    repo = str(task.get("repo") or getattr(args, "repo", "") or "").strip()
    tests = test_commands(args)
    sections = [
        "# DWS + Codex 群反馈处理任务",
        "",
        "你是一个产品经理型个人代理的 Codex 子任务。先做需求/BUG 评审，再决定是否实现。",
        "",
        "## 运行模式",
        f"- mode: {mode}",
        "- listener 不直接改代码；所有代码动作都必须发生在这个 Codex 子任务里。",
        "- 任何涉及权限、支付、生产配置、数据删除、发布开关、不可逆操作的事项都必须停止并要求人工确认。",
        "- 如果信息不足，输出需要追问的问题，不要假装修复。",
        "",
        "## 群消息",
        "```json",
        json.dumps({
            "project": classification.project,
            "group_name": getattr(args, "group_name", ""),
            "conversation_id": event.get("conversation_id", ""),
            "sender": event.get("sender", ""),
            "message_id": event.get("message_id", ""),
            "event_id": event.get("event_id", ""),
            "content": event.get("content", ""),
            "event_time": event.get("event_time", ""),
        }, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Intake Classification",
        "```json",
        json.dumps(classification.to_dict(), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Repo",
        repo or "(missing repo)",
        "",
        "## Acceptance",
    ]
    for item in task.get("acceptance") or []:
        sections.append(f"- {item}")
    sections.extend(["", "## Verification Commands"])
    if tests:
        for command in tests:
            sections.append(f"- `{command}`")
    else:
        sections.append("- 未配置测试命令；不得自动修改代码，只能评估并说明缺口。")

    sections.extend([
        "",
        "## Required Output",
        "- 先列出改动面评估：涉及模块、风险、是否需要人工确认。",
        "- 若需要追问，用 `NEEDS_INFO:` 开头并列出问题。",
        "- 若无法自动处理，用 `BLOCKED:` 开头并说明原因。",
        "- 若已修复，用 `FIXED:` 开头，说明修了什么、运行了哪些测试、当前是已发布还是待发布。",
    ])
    if mode == "guarded":
        sections.extend([
            "",
            "## Guarded Mode Rule",
            "本次只做评审、定位、追问和方案，不要修改文件。",
        ])
    elif mode == "autofix":
        sections.extend([
            "",
            "## Autofix Rule",
            "只有当你确认这是低风险明确 BUG，且能运行上面的测试命令时，才允许修改代码。",
            "如果评估后发现风险升高、改动面不小或测试路径不足，停止并输出 BLOCKED 或 NEEDS_INFO。",
        ])
    return "\n".join(sections).strip() + "\n"


def build_codex_command(
    args: argparse.Namespace,
    classification: ClassificationResult,
    task_dir: Path,
) -> list[str]:
    repo = str(classification.codex_task.get("repo") or getattr(args, "repo", "") or "").strip()
    cmd = [args.codex_bin, "exec", "--cd", repo, "--json", "--output-last-message", str(task_dir / "last_message.md")]
    model = str(getattr(args, "codex_model", "") or "").strip()
    if model:
        cmd.extend(["--model", model])
    if effective_mode(args) == "autofix":
        cmd.extend(["--sandbox", "workspace-write"])
    else:
        cmd.extend(["--sandbox", "read-only"])
    cmd.append("-")
    return cmd


def extract_thread_id_from_jsonl(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                thread_id = str(item.get("thread_id", "")).strip()
                if item.get("type") == "thread.started" and thread_id:
                    return thread_id
    except OSError:
        return ""
    return ""


def build_codex_session_prompt(
    event: dict[str, Any],
    classification: ClassificationResult,
    args: argparse.Namespace,
) -> str:
    code_allowed = code_scope_allowed(event, args)
    repo = str(getattr(args, "repo", "") or "").strip()
    lines = [
        "# 钉钉群 Codex 个人代理 Session",
        "",
        "你正在作为这个钉钉群的长期 Codex 代理回复消息。你可以利用本 Codex session 的历史上下文。",
        "",
        "## 当前授权边界",
        f"- 群：{getattr(args, 'group_name', '') or event.get('conversation_id', '')}",
        f"- 项目：{getattr(args, 'project', '')}",
        f"- 允许读取仓库：{repo or '(未授权仓库)'}",
        "- 文件权限：只读。禁止修改、创建、删除、格式化、提交、推送、发布或执行任何有副作用的操作。",
        "- 若需要本地探查，只能运行只读命令，例如查看文件、搜索文本、查看 git 状态、读取日志或文档。",
        "- 如果信息不足，直接在回复里说明你正在/需要确认什么，不要假装已经完成。",
        "- 最终输出只能是要发送到钉钉群里的中文正文，不要输出 JSON、Markdown 标题、内部推理、命令日志或 session 说明。",
        "",
        "## 代码探查门禁",
        f"- 本条消息是否命中项目关键词：{'yes' if code_allowed else 'no'}",
        f"- 关键词：{', '.join(getattr(args, 'code_keyword', ()) or []) or '(未配置)'}",
        "- 只有命中关键词时，才可以把问题当作抓虾项目问题并读取授权仓库。",
        "",
        "## 最新群消息",
        "```json",
        json.dumps({
            "sender": event.get("sender", ""),
            "conversation_id": event.get("conversation_id", ""),
            "message_id": event.get("message_id", ""),
            "event_id": event.get("event_id", ""),
            "event_time": event.get("event_time", ""),
            "content": event.get("content", ""),
        }, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Worker 分类",
        "```json",
        json.dumps(classification.to_dict(), ensure_ascii=False, indent=2),
        "```",
        "",
        "请基于以上上下文给出一条自然、负责、有连续性的群回复。若这条消息是在追问前一条进度，要结合 session 历史继续回答，不要只说“稍后回复”。",
    ]
    return "\n".join(lines).strip() + "\n"


def build_codex_session_command(
    event: dict[str, Any],
    args: argparse.Namespace,
    thread_id: str,
    session_dir: Path,
) -> list[str]:
    last_message_path = session_dir / "last_message.md"
    model = str(getattr(args, "codex_model", "") or "").strip()
    if thread_id:
        cmd = [
            args.codex_bin,
            "exec",
            "resume",
            "--json",
            "--output-last-message",
            str(last_message_path),
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.extend([thread_id, "-"])
        return cmd

    repo = str(getattr(args, "repo", "") or "").strip()
    cwd = repo if repo_available(repo) else str(Path.cwd())
    cmd = [
        args.codex_bin,
        "exec",
        "--cd",
        cwd,
        "--sandbox",
        "read-only",
        "--json",
        "--output-last-message",
        str(last_message_path),
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.append("-")
    return cmd


def generate_codex_session_reply(
    event: dict[str, Any],
    classification: ClassificationResult,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any]]:
    session_dir = codex_session_dir_for_event(event, args)
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_codex_session_prompt(event, classification, args)
    event_key = event_key_for_dedupe(event).replace(":", "_")
    prompt_path = session_dir / f"{event_key}.prompt.md"
    stdout_path = session_dir / f"{event_key}.codex.jsonl"
    stderr_path = session_dir / f"{event_key}.codex.err.log"
    prompt_path.write_text(prompt, encoding="utf-8")

    thread_id = load_codex_session_id(event, args)
    cmd = build_codex_session_command(event, args, thread_id, session_dir)
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(
            cmd,
            input=prompt,
            stdout=stdout,
            stderr=stderr,
            text=True,
            timeout=args.codex_timeout,
            check=False,
        )
    new_thread_id = extract_thread_id_from_jsonl(stdout_path) or thread_id
    save_codex_session_id(event, args, new_thread_id)
    last_path = session_dir / "last_message.md"
    reply = ""
    if last_path.exists():
        reply = last_path.read_text(encoding="utf-8", errors="replace").strip()
    if completed.returncode != 0:
        raise RuntimeError(f"codex session exited {completed.returncode}; logs: {stderr_path}")
    if not reply:
        raise RuntimeError(f"codex session produced empty reply; logs: {stdout_path}")
    return clip_reply(reply, args.max_reply_chars), {
        "thread_id": new_thread_id,
        "session_dir": str(session_dir),
        "prompt_path": str(prompt_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "resumed": bool(thread_id),
    }


def summarize_codex_last_message(text: str, max_chars: int = 420) -> str:
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def build_delivery_message(classification: ClassificationResult, result: CodexBridgeResult) -> str:
    if result.status == "planned":
        return "已收到，我已经把这条反馈整理成 Codex 评审任务，先评估改动面和验证路径。"
    if result.status == "blocked":
        if result.reason == "codex_execute_disabled":
            return "已收到，这条需要进入 Codex 评审；当前未开启自动执行，我会先保留为待评审任务。"
        if result.reason == "test_commands_missing":
            return "已收到，判断为可能需要代码处理；但项目没有配置测试命令，先转人工确认验证路径。"
        if result.reason == "repo_missing_or_unavailable":
            return "已收到，判断为可能需要代码处理；但没有可用仓库映射，先转人工确认项目归属。"
        return "已收到，这条反馈需要人工确认后继续处理。"
    if result.status == "failed":
        return "Codex 处理失败了，我先保留现场，稍后会根据日志继续排查。"
    if result.status == "completed":
        summary = summarize_codex_last_message(result.last_message)
        if summary:
            return f"已完成 Codex 处理。{summary} 当前状态：本地已处理，待发布/合并后交付。"
        return "已完成 Codex 处理。当前状态：本地已处理，待发布/合并后交付。"
    return classification.reply_draft or "收到，我先处理。"


def run_codex_bridge(event: dict[str, Any], classification: ClassificationResult, args: argparse.Namespace) -> CodexBridgeResult:
    task_id = task_id_for_event(event, classification)
    task_dir = codex_task_dir(args, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_codex_prompt(event, classification, args)
    (task_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    write_json_file(task_dir / "event.json", event)
    write_json_file(task_dir / "classification.json", classification.to_dict())

    mode = effective_mode(args)
    eligible, autofix_reason = is_autofix_eligible(classification, args)
    if mode == "autofix" and not eligible:
        result = CodexBridgeResult(
            task_id=task_id,
            status="blocked",
            task_dir=str(task_dir),
            reason=autofix_reason,
            delivery_message="",
        )
        result = CodexBridgeResult(**{**result.to_dict(), "delivery_message": build_delivery_message(classification, result)})
        write_json_file(task_dir / "status.json", result.to_dict())
        return result

    can_review, review_reason = should_run_codex_review(classification, args)
    if not can_review:
        status = "blocked" if mode in {"guarded", "autofix"} else "planned"
        result = CodexBridgeResult(
            task_id=task_id,
            status=status,
            task_dir=str(task_dir),
            reason=review_reason,
            delivery_message="",
        )
        result = CodexBridgeResult(**{**result.to_dict(), "delivery_message": build_delivery_message(classification, result)})
        write_json_file(task_dir / "status.json", result.to_dict())
        return result

    cmd = build_codex_command(args, classification, task_dir)
    stdout_path = task_dir / "codex.jsonl"
    stderr_path = task_dir / "codex.err.log"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(
            cmd,
            input=prompt,
            stdout=stdout,
            stderr=stderr,
            text=True,
            timeout=args.codex_timeout,
            check=False,
        )
    last_message = ""
    last_path = task_dir / "last_message.md"
    if last_path.exists():
        last_message = last_path.read_text(encoding="utf-8", errors="replace")
    result = CodexBridgeResult(
        task_id=task_id,
        status="completed" if completed.returncode == 0 else "failed",
        task_dir=str(task_dir),
        reason="" if completed.returncode == 0 else f"codex_exit_{completed.returncode}",
        command=tuple(cmd),
        executed=True,
        returncode=completed.returncode,
        last_message=last_message,
        delivery_message="",
    )
    result = CodexBridgeResult(**{**result.to_dict(), "delivery_message": build_delivery_message(classification, result)})
    write_json_file(task_dir / "status.json", result.to_dict())
    return result


def log_json(record: dict[str, Any]) -> None:
    print(json.dumps(record, ensure_ascii=False), flush=True)


def start_stderr_pump(process: subprocess.Popen[str], ready: threading.Event, label: str = "") -> threading.Thread:
    def pump() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            if "[event] ready " in line:
                ready.set()
            if label:
                print(f"[{label}] {line}", file=sys.stderr, end="", flush=True)
            else:
                print(line, file=sys.stderr, end="", flush=True)

    thread = threading.Thread(target=pump, name="dws-event-stderr", daemon=True)
    thread.start()
    return thread


def start_stdout_pump(consumer: ManagedConsumer, seen: set[str], lock: threading.Lock) -> threading.Thread:
    def pump() -> None:
        assert consumer.process.stdout is not None
        for line in consumer.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"WARN: {consumer.label}: skip invalid event JSON: {e}", file=sys.stderr)
                continue
            try:
                with lock:
                    process_event(event, seen, consumer.args)
            except Exception as e:  # keep worker alive across bad events
                print(f"ERROR: {consumer.label}: process event failed: {e}", file=sys.stderr)

    thread = threading.Thread(target=pump, name=f"{consumer.label}-stdout", daemon=True)
    thread.start()
    return thread


def wait_until_ready(process: subprocess.Popen[str], ready: threading.Event, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.is_set():
            return
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"dws event consume exited before ready: {code}")
        time.sleep(0.05)
    raise TimeoutError(f"dws event consume did not become ready within {timeout:g}s")


def admission_failure(event: dict[str, Any], args: argparse.Namespace) -> str:
    allowed = set(getattr(args, "admit_conversation", set()) or set())
    if not allowed:
        return ""
    conversation_id = str(event.get("conversation_id", "")).strip()
    if not conversation_id:
        return "missing_conversation_id"
    if conversation_id not in allowed:
        return "conversation_not_admitted"
    return ""


def generate_dialogue_reply(
    event: dict[str, Any],
    classification: ClassificationResult,
    args: argparse.Namespace,
) -> str:
    try:
        return generate_reply(event, args)
    except Exception as e:
        if not bool(getattr(args, "reply_fallback", True)):
            raise
        print(f"WARN: AI reply failed, using classifier draft: {e}", file=sys.stderr)
        return classification.reply_draft or default_reply_for_type(classification.type) or "收到，我先确认一下，稍后回复。"


def code_ack_message(classification: ClassificationResult) -> str:
    if classification.reply_draft:
        return classification.reply_draft
    return default_reply_for_type(classification.type) or "收到，这条需要进入 Codex 评审，我先评估改动面。"


def process_event(event: dict[str, Any], seen: set[str], args: argparse.Namespace) -> None:
    event_type = str(event.get("type") or args.event_key)
    if event_type not in RECEIVE_EVENTS:
        log_json({"action": "skipped", "reason": "not_message_receive_event", "type": event_type})
        return

    key = event_key_for_dedupe(event)
    if key in seen:
        log_json({"action": "skipped", "reason": "duplicate", "key": key})
        return

    denied = admission_failure(event, args)
    if denied:
        log_json({
            "action": "skipped",
            "reason": denied,
            "event_id": event.get("event_id", ""),
            "conversation_id": event.get("conversation_id", ""),
            **context_fields(args),
        })
        append_seen(args.state_file, key, "skipped_" + denied)
        seen.add(key)
        return

    content = str(event.get("content", "")).strip()
    if not content:
        log_json({"action": "skipped", "reason": "empty_content", "event_id": event.get("event_id", "")})
        append_seen(args.state_file, key, "skipped_empty_content")
        seen.add(key)
        return

    target = resolve_reply_target(event, args.event_key, args.reply_target)
    if args.send and not target_allowed(target, args):
        log_json({
            "action": "skipped",
            "reason": "target_not_allowed",
            "event_id": event.get("event_id", ""),
            "target": {"flag": target.flag, "value": target.value},
        })
        append_seen(args.state_file, key, "skipped_target_not_allowed")
        seen.add(key)
        return

    classification = classify_feedback(event, args)
    record = {
        "event_id": event.get("event_id", ""),
        "message_id": event.get("message_id", ""),
        "sender": event.get("sender", ""),
        "target": {"flag": target.flag, "value": target.value},
        "classification": classification.to_dict(),
        **context_fields(args),
    }
    mode = effective_mode(args)

    if classification.type == "noise":
        log_json({"action": "skipped", "reason": "noise", **record})
        append_seen(args.state_file, key, "skipped_noise")
        seen.add(key)
        return

    if mode == "observe":
        log_json({"action": "classified", **record})
        append_seen(args.state_file, key, "observed")
        seen.add(key)
        return

    if getattr(args, "reply_engine", "ai") == "codex":
        try:
            reply, session_result = generate_codex_session_reply(event, classification, args)
        except Exception as e:
            reply = "我这边 Codex 只读会话暂时没跑通，先保留这条消息，稍后继续确认。"
            session_result = {"error": str(e)}
            print(f"ERROR: codex session reply failed: {e}", file=sys.stderr)
        record["reply"] = reply
        record["codex_session"] = session_result
        if not args.send:
            log_json({"action": "codex_session_draft", **record})
            append_seen(args.state_file, key, "codex_session_drafted")
            seen.add(key)
            return
        result = send_reply(event, target, reply, args)
        log_json({"action": "codex_session_sent", **record, "send_result": result})
        append_seen(args.state_file, key, "codex_session_sent")
        seen.add(key)
        return

    if classification.needs_code:
        ack = code_ack_message(classification)
        if mode == "draft":
            log_json({"action": "codex_draft", **record, "reply": ack, "codex_prompt": build_codex_prompt(event, classification, args)})
            append_seen(args.state_file, key, "codex_drafted")
            seen.add(key)
            return

        send_result: dict[str, Any] = {}
        if args.send:
            send_result = send_reply(event, target, ack, args, purpose="ack")
        bridge_result = run_codex_bridge(event, classification, args)
        delivery_result: dict[str, Any] = {}
        if args.send and bridge_result.delivery_message:
            delivery_result = send_reply(event, target, bridge_result.delivery_message, args, purpose="delivery")
        log_json({
            "action": "codex_" + bridge_result.status,
            **record,
            "reply": ack,
            "ack_send_result": send_result,
            "codex_result": bridge_result.to_dict(),
            "delivery_send_result": delivery_result,
        })
        append_seen(args.state_file, key, "codex_" + bridge_result.status)
        seen.add(key)
        return

    reply = generate_dialogue_reply(event, classification, args)
    record["reply"] = reply
    if not args.send:
        log_json({"action": "draft", **record})
        append_seen(args.state_file, key, "drafted")
        seen.add(key)
        return

    result = send_reply(event, target, reply, args)
    log_json({"action": "sent", **record, "send_result": result})
    append_seen(args.state_file, key, "sent")
    seen.add(key)


def run(args: argparse.Namespace) -> int:
    args.state_file = Path(args.state_file).expanduser()
    if args.config:
        return run_config(args)
    if args.event_key not in RECEIVE_EVENTS:
        raise SystemExit(f"--event-key must be one of: {', '.join(sorted(RECEIVE_EVENTS))}")
    if args.send and not has_send_scope(args):
        raise SystemExit(
            "--send requires --group, --open-dingtalk-id, --allow-conversation, "
            "--allow-sender, or --allow-any-target"
        )

    seen = load_seen(args.state_file)
    cmd = build_consume_command(args)
    if args.print_command:
        log_json({"action": "consume_command", "command": cmd})

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    ready = threading.Event()
    start_stderr_pump(process, ready)

    def stop_child(signum: int, _frame: Any) -> None:
        if process.poll() is None:
            process.terminate()

    previous_sigterm = signal.signal(signal.SIGTERM, stop_child)
    previous_sigint = signal.signal(signal.SIGINT, stop_child)
    try:
        wait_until_ready(process, ready, args.ready_timeout)
        send_ready_message(args)
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"WARN: skip invalid event JSON: {e}", file=sys.stderr)
                continue
            try:
                process_event(event, seen, args)
            except Exception as e:  # keep worker alive across bad events
                print(f"ERROR: process event failed: {e}", file=sys.stderr)
        return process.wait()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def terminate_consumers(consumers: list[ManagedConsumer]) -> None:
    for consumer in consumers:
        if consumer.process.poll() is None:
            consumer.process.terminate()
    for consumer in consumers:
        if consumer.process.poll() is None:
            try:
                consumer.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                consumer.process.kill()
                consumer.process.wait()


def run_config(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    defaults, groups = load_agent_config(config_path)
    seen = load_seen(args.state_file)
    seen_lock = threading.Lock()
    consumers: list[ManagedConsumer] = []

    for group in groups:
        group_args = args_for_group(args, defaults, group)
        cmd = build_consume_command(group_args)
        if args.print_command or args.config_check:
            log_json({
                "action": "consume_command",
                "name": group.name,
                "project": group.project,
                "group": group.group_id,
                "mode": group.mode,
                "send": group_args.send,
                "classifier": group_args.classifier,
                "reply_engine": group_args.reply_engine,
                "repo": group.repo,
                "code_keywords": list(group.code_keywords),
                "test_commands": list(group.test_commands),
                "codex_execute": group_args.codex_execute,
                "command": cmd,
            })
        if args.config_check:
            continue
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        ready = threading.Event()
        label = group.project or group.name or group.group_id
        start_stderr_pump(process, ready, label)
        consumers.append(ManagedConsumer(label=label, args=group_args, process=process, ready=ready))

    if args.config_check:
        return 0

    def stop_children(_signum: int, _frame: Any) -> None:
        terminate_consumers(consumers)

    previous_sigterm = signal.signal(signal.SIGTERM, stop_children)
    previous_sigint = signal.signal(signal.SIGINT, stop_children)
    try:
        for consumer in consumers:
            wait_until_ready(consumer.process, consumer.ready, args.ready_timeout)
        for consumer in consumers:
            send_ready_message(consumer.args)
        for consumer in consumers:
            consumer.stdout_thread = start_stdout_pump(consumer, seen, seen_lock)

        while True:
            all_done = True
            for consumer in consumers:
                code = consumer.process.poll()
                if code is None:
                    all_done = False
                    continue
                if code != 0:
                    print(f"ERROR: {consumer.label}: dws event consume exited with {code}", file=sys.stderr)
                    terminate_consumers(consumers)
                    return code
            if all_done:
                break
            time.sleep(0.5)
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
        terminate_consumers(consumers)
        for consumer in consumers:
            if consumer.process.stdin:
                try:
                    consumer.process.stdin.close()
                except OSError:
                    pass
        for consumer in consumers:
            if consumer.stdout_thread is not None:
                consumer.stdout_thread.join(timeout=1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DingTalk AI auto-reply worker")
    parser.add_argument("--config", default="", help="JSON allowlist config for project groups")
    parser.add_argument("--config-check", action="store_true",
                        help="validate config and print consume commands without starting listeners")
    parser.add_argument("--mode", choices=tuple(sorted(AUTOMATION_MODES)), default="draft",
                        help="automation mode: observe, draft, guarded, autofix, or legacy send")
    parser.add_argument("--project", default="", help="project key for classification context")
    parser.add_argument("--repo", default="", help="local repo path for Codex code review/fix tasks")
    parser.add_argument("--code-keyword", action="append", default=[],
                        help="only allow repo/Codex routing when message content contains this keyword")
    parser.add_argument("--reply-engine", choices=("ai", "codex"), default="ai",
                        help="reply generator: ai uses chat completions, codex uses a persistent per-group Codex session")
    parser.add_argument("--group-name", default="", help="human-readable group name for logs/prompts")
    parser.add_argument("--event-key", default="user_im_message_receive_at",
                        help="personal IM receive event key")
    parser.add_argument("--profile", default="", help="dws profile/corp selector")
    parser.add_argument("--group", default="", help="listen to one group openConversationId")
    parser.add_argument("--user", default="", help="listen to one internal userId")
    parser.add_argument("--open-dingtalk-id", default="", help="listen to one openDingtalkId")
    parser.add_argument("--query", default="", help="server-side text query filter")
    parser.add_argument("--filter-json", default="", help="personal event filter JSON")
    parser.add_argument("--duration", default="", help="bounded consume duration, e.g. 55m")
    parser.add_argument("--max-events", type=int, default=0, help="stop after N events")
    parser.add_argument("--reply-target", choices=("auto", "sender", "conversation"), default="auto")
    parser.add_argument("--send", action="store_true", help="actually send replies; default only drafts")
    parser.add_argument("--ready-message", default="",
                        help="send this message after the listener is ready; only to the configured target")
    parser.add_argument("--allow-any-target", action="store_true",
                        help="allow --send to reply to any target produced by the event")
    parser.add_argument("--allow-conversation", action="append", default=[],
                        help="allow one group/openConversationId for --send; repeat or comma-separate")
    parser.add_argument("--allow-sender", action="append", default=[],
                        help="allow one sender openDingtalkId for --send; repeat or comma-separate")
    parser.add_argument("--admit-conversation", action="append", default=[],
                        help="allow one conversation to be processed; repeat or comma-separate")
    parser.add_argument("--state-file", default=str(default_state_file()), help="dedupe state jsonl")
    parser.add_argument("--dws-bin", default=os.getenv("DWS_BIN", "dws"), help="dws executable")
    parser.add_argument("--dws-timeout", type=float, default=60.0, help="seconds for dws send calls")
    parser.add_argument("--ready-timeout", type=float, default=60.0, help="seconds to wait for event ready")
    parser.add_argument("--classifier", choices=("heuristic", "hybrid", "ai"), default="heuristic",
                        help="feedback classifier; hybrid uses AI only when credentials are present")
    parser.add_argument("--api-key", default="", help="AI API key; prefer DWS_AI_API_KEY env")
    parser.add_argument("--base-url", default="", help="OpenAI-compatible base URL")
    parser.add_argument("--model", default="", help="AI model; prefer DWS_AI_MODEL env")
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--max-reply-chars", type=int, default=500)
    parser.add_argument("--ai-timeout", type=float, default=60.0)
    parser.add_argument("--no-reply-fallback", dest="reply_fallback", action="store_false",
                        help="fail the event instead of using classifier reply_draft when AI reply generation fails")
    parser.add_argument("--codex-bin", default=os.getenv("CODEX_BIN", "codex"), help="codex executable")
    parser.add_argument("--codex-model", default=os.getenv("DWS_CODEX_MODEL", ""), help="optional Codex model override")
    parser.add_argument("--codex-log-dir", default=str(Path.home() / ".dws" / "ai-autoreply" / "codex-tasks"),
                        help="directory for Codex task bundles and logs")
    parser.add_argument("--codex-session-dir", default=str(Path.home() / ".dws" / "ai-autoreply" / "codex-sessions"),
                        help="directory for persistent per-group Codex session metadata and logs")
    parser.add_argument("--codex-timeout", type=float, default=3600.0, help="seconds for one Codex task")
    parser.add_argument("--codex-execute", action="store_true",
                        help="actually start codex exec for guarded/autofix code tasks")
    parser.add_argument("--test-command", action="append", default=[],
                        help="verification command required for autofix; repeat for multiple commands")
    parser.add_argument("--autofix-min-confidence", type=float, default=0.85,
                        help="minimum classification confidence for autofix")
    parser.add_argument("--print-command", action="store_true", help="print the dws consume command")
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.allow_conversation = parse_csv_values(args.allow_conversation)
    args.allow_sender = parse_csv_values(args.allow_sender)
    args.admit_conversation = parse_csv_values(args.admit_conversation)
    args.code_keyword = tuple(parse_csv_values(args.code_keyword))
    args.reply_engine = str(args.reply_engine or "ai")
    args.mode = normalize_mode(args.mode)
    args.test_command = tuple(str(item).strip() for item in args.test_command if str(item).strip())
    if args.group:
        args.admit_conversation.add(args.group)
    return args


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = normalize_args(parser.parse_args(argv))
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
