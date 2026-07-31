#!/usr/bin/env python3

import io
import json
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_fenshen import worker


class AutoReplyWorkerTest(unittest.TestCase):
    def parse(self, argv):
        return worker.normalize_args(worker.build_parser().parse_args(argv))

    def test_build_consume_command_uses_flatten_ndjson_and_scope(self):
        args = self.parse([
            "--dws-bin", "/usr/local/bin/dws",
            "--profile", "corp:user",
            "--event-key", "user_im_message_receive_group",
            "--group", "cid-1",
            "--query", "urgent,help",
            "--duration", "55m",
            "--max-events", "3",
        ])

        self.assertEqual(worker.build_consume_command(args), [
            "/usr/local/bin/dws",
            "--profile", "corp:user",
            "event", "consume", "user_im_message_receive_group",
            "--flatten", "-f", "ndjson",
            "--group", "cid-1",
            "--query", "urgent,help",
            "--duration", "55m",
            "--max-events", "3",
        ])

    def test_resolve_reply_target_auto_group_and_sender(self):
        group_event = {
            "type": "user_im_message_receive_group",
            "conversation_id": "cid-1",
            "sender_open_dingtalk_id": "open-1",
        }
        self.assertEqual(
            worker.resolve_reply_target(group_event, "user_im_message_receive_group", "auto"),
            worker.ReplyTarget("--group", "cid-1"),
        )

        o2o_event = {
            "type": "user_im_message_receive_o2o",
            "conversation_id": "cid-o2o",
            "sender_open_dingtalk_id": "open-2",
        }
        self.assertEqual(
            worker.resolve_reply_target(o2o_event, "user_im_message_receive_o2o", "auto"),
            worker.ReplyTarget("--open-dingtalk-id", "open-2"),
        )

    def test_send_requires_explicit_scope(self):
        args = self.parse(["--send"])
        self.assertFalse(worker.has_send_scope(args))

        args = self.parse(["--send", "--allow-conversation", "cid-1,cid-2"])
        self.assertTrue(worker.has_send_scope(args))
        self.assertTrue(worker.target_allowed(worker.ReplyTarget("--group", "cid-2"), args))
        self.assertFalse(worker.target_allowed(worker.ReplyTarget("--group", "cid-x"), args))

    def test_extract_reply_accepts_chat_completion_shape(self):
        self.assertEqual(
            worker.extract_reply({
                "choices": [
                    {"message": {"content": "  好的，我稍后看一下。  "}},
                ],
            }),
            "好的，我稍后看一下。",
        )

    def test_clip_reply(self):
        self.assertEqual(worker.clip_reply("abcdef", 4), "abcd...")
        self.assertEqual(worker.clip_reply("abcdef", 0), "abcdef")

    def test_build_send_command_uses_uuid_and_profile(self):
        target = worker.ReplyTarget("--open-dingtalk-id", "open-1")
        cmd = worker.build_send_command("dws", "corp:user", target, "收到", "uuid-1")
        self.assertEqual(cmd, [
            "dws", "--profile", "corp:user",
            "chat", "message", "send",
            "--open-dingtalk-id", "open-1",
            "--text", "收到",
            "--uuid", "uuid-1",
            "--format", "json",
        ])

    def test_seen_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "seen.jsonl"
            worker.append_seen(path, "event-1", "drafted")
            self.assertEqual(worker.load_seen(path), {"event-1"})

    def test_process_event_draft_writes_seen(self):
        args = self.parse(["--event-key", "user_im_message_receive_o2o"])
        event = {
            "type": "user_im_message_receive_o2o",
            "event_id": "event-1",
            "message_id": "msg-1",
            "sender": "张三",
            "sender_open_dingtalk_id": "open-1",
            "content": "在吗",
        }
        with tempfile.TemporaryDirectory() as tmp:
            args.state_file = pathlib.Path(tmp) / "seen.jsonl"
            previous = worker.generate_reply
            worker.generate_reply = lambda _event, _args: "在的，我稍后回复。"
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    worker.process_event(event, set(), args)
            finally:
                worker.generate_reply = previous

            record = json_loads_line(buf.getvalue())
            self.assertEqual(record["action"], "draft")
            self.assertEqual(record["reply"], "在的，我稍后回复。")
            self.assertEqual(worker.load_seen(args.state_file), {"event-1"})

    def test_process_event_send_skips_disallowed_target(self):
        args = self.parse(["--event-key", "user_im_message_receive_group", "--send"])
        event = {
            "type": "user_im_message_receive_group",
            "event_id": "event-2",
            "message_id": "msg-2",
            "conversation_id": "cid-not-allowed",
            "sender_open_dingtalk_id": "open-1",
            "content": "帮我看一下",
        }
        with tempfile.TemporaryDirectory() as tmp:
            args.state_file = pathlib.Path(tmp) / "seen.jsonl"
            buf = io.StringIO()
            with redirect_stdout(buf):
                worker.process_event(event, set(), args)

            record = json_loads_line(buf.getvalue())
            self.assertEqual(record["action"], "skipped")
            self.assertEqual(record["reason"], "target_not_allowed")
            self.assertEqual(worker.load_seen(args.state_file), {"event-2"})

    def test_load_agent_config_parses_enabled_group_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "defaults": {"mode": "draft", "profile": "corp:user"},
                "groups": [
                    {
                        "name": "抓虾反馈群",
                        "group": "cid-crawshrimp",
                        "project": "crawshrimp",
                        "repo": "/Users/xingyicheng/Documents/crawshrimp",
                        "query": "问题,反馈,bug",
                        "ready_message": "我在了",
                    },
                    {
                        "name": "disabled",
                        "group": "cid-disabled",
                        "enabled": False,
                    },
                ],
            }), encoding="utf-8")

            defaults, groups = worker.load_agent_config(path)
            self.assertEqual(defaults["profile"], "corp:user")
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].name, "抓虾反馈群")
            self.assertEqual(groups[0].group_id, "cid-crawshrimp")
            self.assertEqual(groups[0].project, "crawshrimp")
            self.assertEqual(groups[0].ready_message, "我在了")

    def test_args_for_group_sets_admission_and_send_allowlist(self):
        base = self.parse(["--config", "config.json"])
        group = worker.GroupConfig(
            name="Listingify 反馈群",
            group_id="cid-listingify",
            project="listingify",
            repo="/Users/xingyicheng/Documents/Listingify",
            send=True,
            ready_message="我在了",
        )

        args = worker.args_for_group(base, {}, group)

        self.assertEqual(args.event_key, "user_im_message_receive_group")
        self.assertEqual(args.group, "cid-listingify")
        self.assertTrue(args.send)
        self.assertEqual(args.allow_conversation, {"cid-listingify"})
        self.assertEqual(args.admit_conversation, {"cid-listingify"})
        self.assertEqual(args.project, "listingify")
        self.assertEqual(args.ready_message, "我在了")

    def test_build_ready_message_command_targets_configured_group(self):
        args = self.parse([
            "--group", "cid-1",
            "--profile", "corp:user",
            "--ready-message", "我在了",
        ])

        cmd = worker.build_ready_message_command(args)

        self.assertEqual(cmd[:7], [
            "dws", "--profile", "corp:user",
            "chat", "message", "send",
            "--group",
        ])
        self.assertEqual(cmd[7], "cid-1")
        self.assertIn("--text", cmd)
        self.assertEqual(cmd[cmd.index("--text") + 1], "我在了")
        self.assertIn("--uuid", cmd)

    def test_duplicate_uuid_payload_treats_idempotent_send_as_success(self):
        payload = worker.duplicate_uuid_payload(
            "Request is repeated with uuid 'ai-autoreply-ready-abc123'."
        )

        self.assertEqual(payload, {
            "duplicate_uuid": True,
            "uuid": "ai-autoreply-ready-abc123",
        })

    def test_process_event_skips_not_admitted_conversation_before_ai(self):
        args = self.parse([
            "--event-key", "user_im_message_receive_group",
            "--admit-conversation", "cid-allowed",
        ])
        event = {
            "type": "user_im_message_receive_group",
            "event_id": "event-3",
            "message_id": "msg-3",
            "conversation_id": "cid-other",
            "content": "页面报错",
        }
        with tempfile.TemporaryDirectory() as tmp:
            args.state_file = pathlib.Path(tmp) / "seen.jsonl"
            previous = worker.generate_reply
            worker.generate_reply = lambda _event, _args: self.fail("AI should not be called")
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    worker.process_event(event, set(), args)
            finally:
                worker.generate_reply = previous

            record = json_loads_line(buf.getvalue())
            self.assertEqual(record["action"], "skipped")
            self.assertEqual(record["reason"], "conversation_not_admitted")
            self.assertEqual(worker.load_seen(args.state_file), {"event-3"})

    def test_heuristic_classifier_maps_question_without_code(self):
        args = self.parse(["--project", "crawshrimp", "--repo", "/tmp/repo"])
        event = {"content": "这个鞋包图片怎么重新生成？", "message_id": "msg-q"}

        result = worker.classify_feedback(event, args)

        self.assertEqual(result.type, "support")
        self.assertFalse(result.needs_code)
        self.assertEqual(result.risk, "low")

    def test_heuristic_classifier_maps_feature_to_codex_task(self):
        args = self.parse([
            "--project", "listingify",
            "--repo", "/Users/xingyicheng/Documents/Listingify",
            "--test-command", "npm test",
        ])
        event = {
            "content": "希望 Listingify 上新草稿支持批量选择尺码表",
            "event_id": "evt-feature",
            "message_id": "msg-feature",
            "conversation_id": "cid-listingify",
        }

        result = worker.classify_feedback(event, args)

        self.assertEqual(result.type, "feature")
        self.assertTrue(result.needs_code)
        self.assertEqual(result.risk, "medium")
        self.assertEqual(result.codex_task["repo"], "/Users/xingyicheng/Documents/Listingify")
        self.assertEqual(result.codex_task["test_commands"], ["npm test"])

    def test_code_keyword_gate_blocks_unrelated_group_bug_from_repo(self):
        args = self.parse([
            "--project", "crawshrimp",
            "--repo", "/Users/xingyicheng/Documents/crawshrimp",
            "--code-keyword", "抓虾,crawshrimp",
        ])

        unrelated = worker.classify_feedback({"content": "这个页面报错了"}, args)
        related = worker.classify_feedback({"content": "抓虾生成视频报错了"}, args)

        self.assertEqual(unrelated.type, "bug")
        self.assertFalse(unrelated.needs_code)
        self.assertEqual(related.type, "bug")
        self.assertTrue(related.needs_code)
        self.assertEqual(related.codex_task["repo"], "/Users/xingyicheng/Documents/crawshrimp")

    def test_process_event_feature_in_draft_creates_codex_draft_without_ai_reply(self):
        args = self.parse([
            "--event-key", "user_im_message_receive_group",
            "--group", "cid-1",
            "--project", "listingify",
            "--repo", "/Users/xingyicheng/Documents/Listingify",
        ])
        event = {
            "type": "user_im_message_receive_group",
            "event_id": "event-feature",
            "message_id": "msg-feature",
            "conversation_id": "cid-1",
            "sender_open_dingtalk_id": "open-1",
            "content": "希望上新草稿支持批量选择尺码表",
        }
        with tempfile.TemporaryDirectory() as tmp:
            args.state_file = pathlib.Path(tmp) / "seen.jsonl"
            previous = worker.generate_reply
            worker.generate_reply = lambda _event, _args: self.fail("dialogue AI should not be called for code tasks")
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    worker.process_event(event, set(), args)
            finally:
                worker.generate_reply = previous

            record = json_loads_line(buf.getvalue())
            self.assertEqual(record["action"], "codex_draft")
            self.assertEqual(record["classification"]["type"], "feature")
            self.assertIn("Codex", record["codex_prompt"])
            self.assertEqual(worker.load_seen(args.state_file), {"event-feature"})

    def test_guarded_code_task_writes_task_bundle_but_does_not_execute_without_flag(self):
        args = self.parse([
            "--event-key", "user_im_message_receive_group",
            "--group", "cid-1",
            "--mode", "guarded",
            "--project", "crawshrimp",
        ])
        event = {
            "type": "user_im_message_receive_group",
            "event_id": "event-bug",
            "message_id": "msg-bug",
            "conversation_id": "cid-1",
            "sender_open_dingtalk_id": "open-1",
            "content": "抓虾生成视频报错，提示 exception",
        }
        with tempfile.TemporaryDirectory() as tmp:
            args.repo = tmp
            args.codex_log_dir = str(pathlib.Path(tmp) / "codex")
            args.state_file = pathlib.Path(tmp) / "seen.jsonl"
            buf = io.StringIO()
            with redirect_stdout(buf):
                worker.process_event(event, set(), args)

            record = json_loads_line(buf.getvalue())
            self.assertEqual(record["action"], "codex_blocked")
            self.assertEqual(record["codex_result"]["reason"], "codex_execute_disabled")
            self.assertTrue(pathlib.Path(record["codex_result"]["task_dir"], "prompt.md").exists())

    def test_autofix_requires_low_risk_bug_repo_tests_and_execute(self):
        args = self.parse([
            "--mode", "autofix",
            "--codex-execute",
            "--test-command", "python3 -m pytest",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            args.repo = tmp
            event = {"content": "明确 BUG：保存失败报错", "message_id": "msg-bug"}
            classification = worker.classify_feedback(event, args)

            ok, reason = worker.is_autofix_eligible(classification, args)

            self.assertTrue(ok, reason)

    def test_autofix_blocks_high_risk_or_missing_tests(self):
        args = self.parse(["--mode", "autofix", "--codex-execute"])
        with tempfile.TemporaryDirectory() as tmp:
            args.repo = tmp
            event = {"content": "明确 BUG：生产数据库删除报错", "message_id": "msg-risk"}
            classification = worker.classify_feedback(event, args)

            ok, reason = worker.is_autofix_eligible(classification, args)

            self.assertFalse(ok)
            self.assertEqual(reason, "risk_not_low")

    def test_build_codex_command_uses_readonly_for_guarded_and_workspace_for_autofix(self):
        with tempfile.TemporaryDirectory() as tmp:
            guarded = self.parse(["--mode", "guarded", "--repo", tmp])
            classification = worker.classify_feedback({"content": "页面报错"}, guarded)
            guarded_cmd = worker.build_codex_command(guarded, classification, pathlib.Path(tmp))
            self.assertIn("read-only", guarded_cmd)

            autofix = self.parse(["--mode", "autofix", "--repo", tmp])
            classification = worker.classify_feedback({"content": "页面报错"}, autofix)
            autofix_cmd = worker.build_codex_command(autofix, classification, pathlib.Path(tmp))
            self.assertIn("workspace-write", autofix_cmd)

    def test_extract_thread_id_from_codex_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "out.jsonl"
            path.write_text(
                '{"type":"thread.started","thread_id":"019fb69d-ce05-7881-8f96-1569517cdc87"}\n',
                encoding="utf-8",
            )

            self.assertEqual(
                worker.extract_thread_id_from_jsonl(path),
                "019fb69d-ce05-7881-8f96-1569517cdc87",
            )

    def test_process_event_codex_reply_engine_uses_session_not_direct_ai(self):
        args = self.parse([
            "--event-key", "user_im_message_receive_group",
            "--group", "cid-1",
            "--send",
            "--reply-engine", "codex",
            "--allow-conversation", "cid-1",
        ])
        event = {
            "type": "user_im_message_receive_group",
            "event_id": "event-codex",
            "message_id": "msg-codex",
            "conversation_id": "cid-1",
            "sender_open_dingtalk_id": "open-1",
            "content": "有结果了吗",
        }
        with tempfile.TemporaryDirectory() as tmp:
            args.state_file = pathlib.Path(tmp) / "seen.jsonl"
            previous_reply = worker.generate_reply
            previous_codex = worker.generate_codex_session_reply
            previous_send = worker.send_reply
            worker.generate_reply = lambda _event, _args: self.fail("direct AI reply should not be called")
            worker.generate_codex_session_reply = (
                lambda _event, _classification, _args: ("Codex session 回复", {"thread_id": "thread-1"})
            )
            worker.send_reply = lambda _event, _target, _reply, _args, purpose="reply": {"success": True}
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    worker.process_event(event, set(), args)
            finally:
                worker.generate_reply = previous_reply
                worker.generate_codex_session_reply = previous_codex
                worker.send_reply = previous_send

            record = json_loads_line(buf.getvalue())
            self.assertEqual(record["action"], "codex_session_sent")
            self.assertEqual(record["reply"], "Codex session 回复")
            self.assertEqual(record["codex_session"]["thread_id"], "thread-1")
            self.assertEqual(worker.load_seen(args.state_file), {"event-codex"})


def json_loads_line(value):
    import json

    return json.loads(value.strip().splitlines()[0])


if __name__ == "__main__":
    unittest.main()
