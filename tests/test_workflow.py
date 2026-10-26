"""Behavioral tests using isolated synthetic observations, never real devices."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("workflow", Path(__file__).resolve().parents[1] / "scripts" / "workflow.py")
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)
NOTE = "本人已核对当前步骤，结果符合说明"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.flow = w.Workflow(Path(self.tmp.name) / "client")

    def init(self, computer="mac", phone="android", goal="subscription", network="self-hosted"):
        return self.flow.init(dict(computer=computer, phone=phone, goal=goal, network=network))

    def finish_current(self):
        for check in self.flow.view()["checks"]:
            if not check["satisfied"]:
                self.flow.record(check["id"], "passed", NOTE)

    def reach(self, stage):
        for _ in range(len(w.STAGES) + 1):
            current = self.flow.view()["stage"]
            if current == stage:
                return
            self.assertNotEqual(current, "complete", stage)
            self.finish_current()
        self.fail("stage did not advance")

    def test_all_device_and_goal_routes(self):
        for computer in w.CHOICES["computer"]:
            for phone in w.CHOICES["phone"]:
                for goal in w.CHOICES["goal"]:
                    for network in w.CHOICES["network"]:
                        if computer == phone == "none":
                            continue
                        with self.subTest(computer=computer, phone=phone, goal=goal, network=network):
                            self.flow = w.Workflow(Path(self.tmp.name) / (computer + phone + goal + network))
                            self.init(computer, phone, goal, network)
                            seen = []
                            for _ in range(len(w.STAGES) + 1):
                                if self.flow.view()["stage"] == "complete":
                                    break
                                seen.append(self.flow.view()["stage"])
                                self.finish_current()
                                # A new object must resume the same observations.
                                self.flow = w.Workflow(self.flow.directory)
                            self.assertEqual(self.flow.view()["stage"], "complete")
                            self.assertEqual("phone" in seen, phone != "none")
                            self.assertEqual("computer" in seen, computer != "none")
                            self.assertEqual("browser" in seen, computer != "none")
                            self.assertEqual("server" in seen, network == "self-hosted")
                            self.assertEqual("subscription" in seen, goal == "subscription")
                            self.assertEqual("payment-preflight" in seen, goal == "subscription")
                            self.assertEqual(seen[0], "intake")
                            self.assertEqual(seen[-1], "handover")

    def test_no_devices_rejected_without_creating_or_overwriting_state(self):
        with self.assertRaises(w.WorkflowError):
            self.init(computer="none", phone="none")
        self.assertFalse(self.flow.directory.exists())
        self.init(computer="none", phone="iphone")
        original = self.flow.path.read_bytes()
        with self.assertRaises(w.WorkflowError):
            self.flow.configure({"phone": "none"})
        self.assertEqual(self.flow.path.read_bytes(), original)

    def test_existing_network_requires_current_availability_and_support_path(self):
        self.init(computer="none", phone="android", network="existing")
        for check in self.flow.view()["checks"]:
            if check["id"] != "intake.existing_network":
                self.flow.record(check["id"], "passed", NOTE)
        self.assertEqual(self.flow.view()["missing"], ["intake.existing_network"])
        self.flow.record("intake.existing_network", "failed", "当前网络不可用，等待本人联系服务方")
        with self.assertRaises(w.WorkflowError):
            self.flow.record("intake.existing_network", "accepted", "客户愿意承担未检查限制", True)
        with self.assertRaises(w.WorkflowError):
            self.flow.record("payment.method", "passed", NOTE)
        self.flow.record("intake.existing_network", "passed", "本人已核对当前网络可用，知道联系管理方的入口")
        self.assertEqual(self.flow.view()["stage"], "payment-preflight")
        self.assertIn("server", self.flow.view()["skipped_stages"])

    def test_subscription_cannot_pass_without_payment_preflight(self):
        self.init()
        self.finish_current()
        self.assertEqual(self.flow.view()["stage"], "payment-preflight")
        self.flow.record("payment.method", "failed", "尚未取得可用付款资料")
        self.assertEqual(self.flow.view()["status"], "blocked")
        with self.assertRaises(w.WorkflowError):
            self.flow.record("server.access", "passed", NOTE)
        with self.assertRaises(w.WorkflowError):
            self.flow.record("payment.method", "accepted", "愿意继续但没有付款资料", True)
        self.assertEqual(self.flow.view()["stage"], "payment-preflight")

    def test_accepted_limit_needs_explicit_flag_and_persists_reason(self):
        self.init()
        self.reach("computer")
        with self.assertRaises(w.WorkflowError):
            self.flow.record("computer.disconnect_limit", "accepted", "客户接受停止客户端后的直连限制")
        self.flow.record("computer.disconnect_limit", "accepted", "客户接受停止客户端后的直连限制", True)
        self.finish_current()
        view = w.Workflow(self.flow.directory).view()
        self.assertEqual(view["stage"], "browser")
        self.assertEqual(view["accepted_limits"][0]["id"], "computer.disconnect_limit")
        self.assertIn("客户接受", view["accepted_limits"][0]["reason"])

    def test_required_security_payment_and_entitlement_cannot_be_accepted(self):
        self.init()
        for cid in ["intake.eligibility", "payment.method", "subscription.payment", "subscription.entitlement"]:
            with self.subTest(cid=cid), self.assertRaises(w.WorkflowError):
                self.flow.record(cid, "accepted", "客户表示接受", True)

    def test_subscription_requires_receipt_and_entitlement(self):
        self.init()
        self.reach("subscription")
        self.flow.record("subscription.payment", "passed", "本人已完成扣款")
        self.flow.record("subscription.receipt", "passed", "本人已核对订单")
        self.flow.record("subscription.renewal", "passed", "本人已了解续费和取消入口")
        self.assertEqual(self.flow.view()["missing"], ["subscription.entitlement"])
        self.flow.record("subscription.entitlement", "pending", "等待权益更新")
        self.assertEqual(self.flow.view()["stage"], "subscription")

    def test_secret_rejection_does_not_write_or_echo_input(self):
        self.init()
        original = self.flow.path.read_bytes()
        rejected = [
            "邮箱 customer@example.com", "https://localhost/callback?code=private",
            "验证码是 987654", "CVV: 987", "密码：abc!", "token = abc123",
            "节点 203.0.113.4", "IPv6 2001:db8::1234", "恢复密钥 ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",
            "路径 /Users/alice/private.txt", "vless://private-stuff", "1234 5678 9012 3456",
            "密码：2026-10-26", "密码：客户本人保管 abc123", "2026-10-26 验证码 987654",
        ]
        for note in rejected:
            with self.subTest(note=note), self.assertRaises(w.WorkflowError) as failure:
                self.flow.record("intake.eligibility", "passed", note)
            self.assertNotIn(note, str(failure.exception))
            self.assertEqual(self.flow.path.read_bytes(), original)

    def test_dates_versions_and_redacted_statuses_can_be_recorded(self):
        self.init()
        for note in [
            "2026-10-26 已核对当前出口", "完成日期2026-10-26，当前出口已核对", "Windows 11 24H2，客户端可用",
            "验证码：由客户本人输入，不保存", "密码：客户本人保管", "CVV：待本人补齐",
        ]:
            with self.subTest(note=note):
                self.flow.record("intake.account_plan", "passed", note)
                self.assertEqual(self.flow.load()["observations"]["intake.account_plan"]["note"], note)

    def test_repeated_init_preserves_state_and_different_init_rejected(self):
        self.init()
        self.flow.record("intake.eligibility", "passed", NOTE)
        original = self.flow.path.read_bytes()
        self.init()
        self.assertEqual(self.flow.path.read_bytes(), original)
        with self.assertRaises(w.WorkflowError):
            self.init(computer="windows")
        self.assertEqual(self.flow.path.read_bytes(), original)

    def test_configure_restarts_observations_and_does_not_preserve_old_passes(self):
        self.init()
        self.reach("handover")
        result = self.flow.configure({"computer": "windows", "phone": "iphone"})
        self.assertGreater(result["invalidated_count"], 0)
        self.assertEqual(result["stage"], "intake")
        self.assertEqual(self.flow.load()["observations"], {})
        self.assertEqual(result["profile"]["computer"], "windows")

    def test_goal_change_enables_payment_preflight_and_invalidates_prior_results(self):
        self.init(goal="free")
        self.reach("complete")
        result = self.flow.configure({"goal": "subscription"})
        self.assertIn("不会修改已有配置、登录状态或免费权益", result["configuration_notice"])
        self.finish_current()
        self.assertEqual(self.flow.view()["stage"], "payment-preflight")

    def test_original_schema_without_network_resumes_without_rewriting(self):
        self.init()
        self.finish_current()
        state = self.flow.load()
        del state["profile"]["network"]
        self.flow.path.write_text(json.dumps(state))
        original = self.flow.path.read_bytes()
        resumed = w.Workflow(self.flow.directory)
        self.assertEqual(resumed.view()["profile"]["network"], "self-hosted")
        self.assertEqual(resumed.view()["stage"], "payment-preflight")
        resumed.init(dict(computer="mac", phone="android", goal="subscription"))
        self.assertEqual(self.flow.path.read_bytes(), original)
        resumed.record("payment.method", "pending", "等待本人补齐付款方式")
        self.assertEqual(json.loads(self.flow.path.read_text())["profile"]["network"], "self-hosted")

    def test_correction_invalidates_downstream_and_idempotent_record_is_safe(self):
        self.init()
        self.reach("login")
        before = self.flow.path.read_bytes()
        self.flow.record("computer.settings", "passed", NOTE)
        self.assertEqual(self.flow.path.read_bytes(), before)
        self.flow.record("computer.settings", "failed", "当前设置已变化，等待重新核对")
        self.assertEqual(self.flow.view()["stage"], "computer")
        self.assertFalse(any(cid.startswith("browser.") for cid in self.flow.load()["observations"]))

    def test_corrupt_state_never_reset_or_overwritten(self):
        self.init()
        for contents in ["{", "[]", '{"schema":999}', '{"schema":1,"complete":true}']:
            self.flow.path.write_text(contents)
            with self.subTest(contents=contents), self.assertRaises(w.WorkflowError):
                self.init()
            self.assertEqual(self.flow.path.read_text(), contents)

    def test_forged_future_result_and_unknown_check_rejected_on_load(self):
        self.init()
        state = self.flow.load()
        state["observations"]["subscription.payment"] = {
            "result": "passed", "note": NOTE, "observed_at": w.utcnow(), "customer_accepted": False,
        }
        self.flow.path.write_text(json.dumps(state))
        with self.assertRaises(w.WorkflowError):
            self.flow.view()
        state["observations"] = {"unknown": state["observations"]["subscription.payment"]}
        self.flow.path.write_text(json.dumps(state))
        with self.assertRaises(w.WorkflowError):
            self.flow.view()

    @unittest.skipIf(os.name == "nt", "POSIX permissions and symlink semantics")
    def test_permissions_and_symlink_protection(self):
        self.init()
        self.assertEqual(stat.S_IMODE(self.flow.path.stat().st_mode), 0o600)
        link = Path(self.tmp.name) / "alias"
        link.symlink_to(self.flow.directory, target_is_directory=True)
        with self.assertRaises(w.WorkflowError):
            w.Workflow(link)
        self.flow.path.unlink()
        self.flow.path.symlink_to(Path(self.tmp.name) / "other")
        with self.assertRaises(w.WorkflowError):
            w.Workflow(self.flow.directory)

    def test_cli_exit_codes_output_and_no_state_on_error(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = w.main(["--state-dir", str(self.flow.directory), "status"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")
        self.assertFalse(self.flow.directory.exists())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = w.main(["--state-dir", str(self.flow.directory), "init", "--computer", "mac", "--phone", "none", "--goal", "free"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["stage"], "intake")
        self.assertEqual(json.loads(output.getvalue())["profile"]["network"], "self-hosted")

    def test_cli_phone_only_existing_network_route(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = w.main(["--state-dir", str(self.flow.directory), "init", "--computer", "none", "--phone", "iphone", "--goal", "free", "--network", "existing"])
        self.assertEqual(result, 0)
        view = json.loads(output.getvalue())
        self.assertEqual(view["skipped_stages"], ["payment-preflight", "server", "computer", "browser", "subscription"])


if __name__ == "__main__":
    unittest.main()
