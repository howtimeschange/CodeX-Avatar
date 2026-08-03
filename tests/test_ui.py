import json
import pathlib
import sys
import tempfile
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from codex_avatar import ui


class DashboardStateTest(unittest.TestCase):
    def test_missing_config_uses_default_without_writing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.json"
            state = ui.DashboardState(path)

            config = state.load_config()

            self.assertIn("defaults", config)
            self.assertIn("groups", config)
            self.assertFalse(path.exists())

    def test_save_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.json"
            state = ui.DashboardState(path)
            config = {
                "defaults": {"mode": "draft"},
                "groups": [
                    {"name": "Demo", "group": "cid_demo", "enabled": True},
                ],
            }

            state.save_config(config)

            self.assertEqual(state.load_config(), config)
            self.assertTrue(path.exists())

    def test_placeholder_groups_only_counts_enabled_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.json"
            state = ui.DashboardState(path)
            config = {
                "groups": [
                    {"name": "Active", "group": "cid_REPLACE_ACTIVE", "enabled": True},
                    {"name": "Off", "group": "cid_REPLACE_OFF", "enabled": False},
                    {"name": "Real", "group": "cid_real", "enabled": True},
                ],
            }

            self.assertEqual(state.placeholder_groups(config), ["Active"])

    def test_run_config_check_uses_worker_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.json"
            state = ui.DashboardState(path, python_bin=sys.executable)
            state.save_config({
                "defaults": {"mode": "draft"},
                "groups": [{"name": "Demo", "group": "cid_demo", "enabled": True}],
            })

            result = state.run_config_check(timeout=10)

            self.assertTrue(result["ok"], result)
            self.assertIn('"action": "consume_command"', result["stdout"])
            self.assertTrue(any(json.loads(line)["action"] == "consume_command" for line in result["stdout"].splitlines()))

    def test_start_worker_rejects_placeholder_group_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.json"
            state = ui.DashboardState(path)
            state.save_config({
                "defaults": {"mode": "draft"},
                "groups": [{"name": "Demo", "group": "cid_REPLACE_DEMO", "enabled": True}],
            })

            with self.assertRaises(ValueError):
                state.start_worker()


if __name__ == "__main__":
    unittest.main()

