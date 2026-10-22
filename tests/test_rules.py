# -*- coding: utf-8 -*-
"""Offline rule tests. Everything here runs with no network and no client
installed: the point is to pin the *judgment rules* that caused real missed
days, not to re-test HTTP.

    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keeper import config, notify, result, state                       # noqa: E402
from keeper.providers import minimax_code as mm                        # noqa: E402
from keeper.providers import qoder_cn as qd                            # noqa: E402
from keeper.providers import workbuddy as wb                           # noqa: E402
from keeper.providers.base import ApiError, CredError, Snapshot        # noqa: E402
from keeper.runner import Runner                                       # noqa: E402


def make_cfg(**over):
    cfg = json.loads(json.dumps(config.DEFAULTS))
    cfg["notify"]["channel"] = "none"
    cfg["providers"] = []
    cfg["timezone_offset_hours"] = 8
    cfg["alert"]["last_slot_minutes"] = 21 * 60
    for k, v in over.items():
        cfg[k] = v
    return cfg


class Recorder(object):
    """Stand-in notifier so tests can assert *whether* a message was sent."""

    def __init__(self):
        self.sent = []

    def send(self, title, content):
        self.sent.append((title, content))
        return True

    def flush(self):
        pass

    def describe(self):
        return "recorder"


class FakeProvider(object):
    def __init__(self, key="fake", label="Fake", script=None):
        self.key = key
        self.label = label
        self.unit = "分"
        self.cfg = {}
        self.log = lambda *a, **k: None
        self.script = list(script or [])
        self.calls = []

    def _pop(self, kind):
        self.calls.append(kind)
        if not self.script:
            raise AssertionError("script exhausted for %s" % kind)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def check(self):
        return self._pop("check")

    def do_claim(self):
        return self._pop("do_claim")


class TestExitCodes(unittest.TestCase):
    def test_combine_prefers_hard_failures(self):
        self.assertEqual(result.EX_CRED, result.combine([0, 2, 4]))
        self.assertEqual(result.EX_API, result.combine([4, 3, 0]))
        self.assertEqual(result.EX_NOACT, result.combine([0, 4]))
        self.assertEqual(result.EX_OK, result.combine([0, 0]))

    def test_no_activity_does_not_hide_credential_failure(self):
        # 4 must never mask 2/3: "nothing today" is benign, "no login" is not.
        self.assertEqual(result.EX_CRED, result.combine([4, 2, 4]))

    def test_state_maps_to_codes(self):
        self.assertEqual(result.EX_OK, result.to_exit_code(result.Result(result.ALREADY)))
        self.assertEqual(result.EX_NOACT,
                         result.to_exit_code(result.Result(result.UNAVAILABLE)))
        self.assertEqual(result.EX_API, result.to_exit_code(result.Result(result.ERROR)))

    def test_unknown_state_rejected(self):
        with self.assertRaises(ValueError):
            result.Result("magical")


class TestRunnerRules(unittest.TestCase):
    KEYS = ("fake", "gate", "boom", "good", "mystery")

    def tearDown(self):
        for key in self.KEYS:                     # keep var/state clean for humans
            try:
                os.remove(state.path_for(key))
            except OSError:
                pass

    def setUp(self):
        self.runner = Runner(make_cfg())
        self.runner.notify = Recorder()

    def test_already_claimed_is_silent_and_zero(self):
        p = FakeProvider(script=[Snapshot(done=True, summary="今日已签")])
        self.runner.providers = [p]
        rc, rows = self.runner.claim_all()
        self.assertEqual(result.EX_OK, rc)
        self.assertEqual("already", rows[0][1].state)
        self.assertEqual([], self.runner.notify.sent)

    def test_fresh_claim_notifies_once(self):
        p = FakeProvider(script=[Snapshot(claimable=True, summary="可领"),
                                 Snapshot(outcome=result.FRESH, gained=100,
                                          summary="已领到手 +100 Credits")])
        self.runner.providers = [p]
        rc, _ = self.runner.claim_all()
        self.assertEqual(result.EX_OK, rc)
        self.assertEqual(1, len(self.runner.notify.sent))
        self.assertTrue(self.runner.notify.sent[0][0].startswith("✅"))

    def test_idempotent_claim_does_not_notify(self):
        p = FakeProvider(script=[Snapshot(claimable=True),
                                 Snapshot(outcome=result.ALREADY,
                                          summary="服务端判定已领（幂等）")])
        self.runner.providers = [p]
        rc, rows = self.runner.claim_all()
        self.assertEqual(result.EX_OK, rc)
        self.assertEqual([], self.runner.notify.sent)
        self.assertEqual("already", rows[0][1].state)

    def test_unavailable_alone_is_not_alerted_early_in_the_day(self):
        p = FakeProvider(script=[Snapshot(claimable=False, done=False,
                                          summary="还没发布")])
        self.runner.providers = [p]
        rc, rows = self.runner.claim_all()
        self.assertEqual(result.EX_NOACT, rc)
        self.assertEqual("unavailable", rows[0][1].state)
        self.assertEqual([], self.runner.notify.sent)

    def test_credential_failure_alerts_only_at_the_last_slot(self):
        p = FakeProvider(script=[CredError("没登录")])
        self.runner.providers = [p]
        self.runner.last_slot = 0                      # "it is the last slot"
        rc, rows = self.runner.claim_all()
        self.assertEqual(result.EX_CRED, rc)
        self.assertEqual(1, len(self.runner.notify.sent))
        self.assertTrue(self.runner.notify.sent[0][0].startswith("⚠️"))

        self.runner.notify = Recorder()
        self.runner.last_slot = 24 * 60                # never: midday run
        p2 = FakeProvider(script=[CredError("没登录")])
        self.runner.providers = [p2]
        rc2, _ = self.runner.claim_all()
        self.assertEqual(result.EX_CRED, rc2)
        self.assertEqual([], self.runner.notify.sent)

    def test_one_provider_crashing_does_not_stop_the_others(self):
        boom = FakeProvider(key="boom", script=[ApiError("500")])
        good = FakeProvider(key="good", script=[Snapshot(done=True)])
        self.runner.providers = [boom, good]
        rc, rows = self.runner.claim_all()
        self.assertEqual(2, len(rows))
        self.assertEqual("error", rows[0][1].state)
        self.assertEqual("already", rows[1][1].state)
        self.assertEqual(result.EX_API, rc)

    def test_state_file_is_never_a_gate(self):
        """A state file that claims 'done today' must not stop a server re-check.

        Regression for the 2026-10-14 missed day: the cycle rolls at 10:00, a
        00:15 test run wrote {day: tomorrow, checked: true}, and the 10:05 slot
        short-circuited on it — a whole day lost in silence.
        """
        p = FakeProvider(key="gate", script=[Snapshot(done=True),
                                             Snapshot(claimable=True),
                                             Snapshot(outcome=result.FRESH, gained=1,
                                                      summary="+1")])
        self.runner.providers = [p]
        self.runner.claim_all()
        rec = state.load("gate")
        self.assertEqual("already", rec["state"])
        self.runner.claim_all()                      # second run must still ask
        self.assertEqual(3, len(p.calls), "server was not re-checked")

    def test_unloadable_provider_reports_error_not_silence(self):
        self.runner.skipped = [("mystery", "unknown provider key")]
        self.runner.providers = []
        rc, rows = self.runner.claim_all()
        self.assertEqual(result.EX_API, rc)
        self.assertEqual("error", rows[0][1].state)


class TestNotifyPlumbing(unittest.TestCase):
    def test_channel_none_counts_as_delivered(self):
        n = notify.Notifier(make_cfg())
        self.assertTrue(n.send("t", "body"))

    def test_webhook_payload_shape_per_host(self):
        for url, expect in [
            ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=X", "msgtype"),
            ("https://oapi.dingtalk.com/robot/send?access_token=X", "msgtype"),
            ("https://www.pushplus.plus/send/TOKEN", "token"),
            ("https://sctapi.ftqq.com/TOKEN.send", "title"),
            ("https://example.com/hook", "text"),
        ]:
            cfg = make_cfg()
            cfg["notify"]["channel"] = "webhook"
            cfg["notify"]["webhook"]["url"] = url
            n = notify.Notifier(cfg)
            if "ftqq" in url:
                _, headers, data = n._webhook_payload("t", "b")
                self.assertIn("application/x-www-form-urlencoded",
                              headers["Content-Type"])
                self.assertIn("desp=", data.decode())
                continue
            _, _, data = n._webhook_payload("t", "b")
            self.assertIn(expect, json.loads(data.decode()))

    def test_pending_queue_caps_and_expires(self):
        cfg = make_cfg()
        cfg["notify"]["pending_max"] = 3
        cfg["notify"]["pending_keep_days"] = 1
        n = notify.Notifier(cfg)
        n._save([{"ts": time.strftime("%Y-%m-%d %H:%M"), "title": "a%d" % i,
                  "content": ""} for i in range(9)])
        self.assertEqual(3, len(n._load()))
        old = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 5 * 86400))
        n._save([{"ts": old, "title": "stale", "content": "x"}])
        n.channel = "hermes"                     # nothing configured → delivery fails
        n.flush()
        self.assertEqual([], [i for i in n._load() if i["title"] == "stale"])

    def test_throttle_waits_after_a_real_send(self):
        cfg = make_cfg()
        cfg["notify"]["gap_seconds"] = 1
        n = notify.Notifier(cfg)
        n.channel = "none"
        n._last_sent = time.time()
        t0 = time.time()
        n._throttle()
        self.assertGreaterEqual(time.time() - t0, 0.5)

    def test_success_payload_without_error_key_is_not_a_failure(self):
        """Regression: reading `out` as the fallback error text used to mark a
        delivered message as failed, so the queue never drained."""
        cfg = make_cfg()
        cfg["notify"]["channel"] = "hermes"
        cfg["notify"]["hermes"]["exe"] = sys.executable      # not hermes; run fails
        cfg["notify"]["hermes"]["targets"] = ["qqbot:1"]
        n = notify.Notifier(cfg)
        out = json.dumps({"ok": True, "delivery_type": "direct"})
        j = json.loads(out)
        self.assertEqual("", str(j.get("error") or ""))


class TestMiniMaxRules(unittest.TestCase):
    def test_required_params_present_and_ordered(self):
        p = mm.MiniMaxCodeProvider(make_cfg(), lambda *a: None)
        names = [k for k, _ in p._params()]
        self.assertEqual("unix", names[4], "unix must sit where the client puts it")
        self.assertEqual("desktop_version", names[7])
        for must in ("device_platform", "biz_id", "app_id", "version_code",
                     "timezone_offset", "is_desktop", "sys_language", "lang",
                     "os_name", "browser_name", "browser_language",
                     "browser_platform", "client"):
            self.assertIn(must, names)
        joined = "&".join("%s=%s" % kv for kv in p._params())
        self.assertNotIn("device_id=", joined)      # machine ids must stay out
        self.assertNotIn("user_id=", joined)

    def test_panel_enums(self):
        p = mm.MiniMaxCodeProvider(make_cfg(), lambda *a: None)
        data = {"scene": 2, "days": [
            {"day_no": 1, "points": 400, "status": 3, "is_today": True},
            {"day_no": 2, "points": 400, "status": 2, "is_today": False},
            {"day_no": 3, "points": 1000, "status": 1, "is_today": False}]}
        day, days, claimed = p._panel(data)
        self.assertEqual(1, day["day_no"])
        self.assertEqual(3, len(days))
        self.assertEqual(1, claimed)
        self.assertEqual("已领", mm._STATUS_NAME[day["status"]])

    def test_no_is_today_row_is_unavailable(self):
        p = mm.MiniMaxCodeProvider(make_cfg(), lambda *a: None)
        day, _days, _c = p._panel({"days": [{"status": 2}]})
        self.assertIsNone(day)

    def test_biz_code_reads_tencent_envelope(self):
        self.assertEqual((0, "ok"), mm.MiniMaxCodeProvider._biz(
            {"base_resp": {"status_code": 0, "status_msg": "ok"}}))
        self.assertEqual((1406010011, "invalid timezone_id"),
                         mm.MiniMaxCodeProvider._biz(
                             {"base_resp": {"status_code": 1406010011,
                                            "status_msg": "invalid timezone_id"}}))
        self.assertEqual((None, ""), mm.MiniMaxCodeProvider._biz({"data": {}}))


class TestWorkBuddyRules(unittest.TestCase):
    def test_400_with_business_code_is_parsed_not_treated_as_transport_error(self):
        import urllib.error
        provider = wb.WorkBuddyProvider(make_cfg(), lambda *a: None)
        body = b'{"code":10001,"message":"already checked in"}'
        err = urllib.error.HTTPError("https://x", 400, "Bad Request", {},
                                     _Reader(body))
        calls = []

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(req.full_url)
            raise err

        orig = sys.modules["urllib.request"].urlopen
        sys.modules["urllib.request"].urlopen = fake_urlopen
        try:
            code, j = provider._api("/daily-checkin", "tok", "uid", "www.codebuddy.cn")
        finally:
            sys.modules["urllib.request"].urlopen = orig
        self.assertEqual(400, code)
        self.assertEqual(10001, j.get("code"))

    def test_already_claimed_maps_to_already_not_error(self):
        provider = wb.WorkBuddyProvider(make_cfg(), lambda *a: None)
        snap = Snapshot(outcome=result.ALREADY, summary="幂等")
        self.assertEqual(result.ALREADY, snap.outcome)
        self.assertIsNone(snap.gained)


class TestQoderRules(unittest.TestCase):
    def test_campaign_summary_and_claimable_pick(self):
        payload = {"campaigns": [
            {"campaignKey": "act-19700101-250", "campaignId": "a",
             "actionType": "OTHER", "claimStatus": "CLAIMABLE"},
            {"campaignKey": "act-19700101-603", "campaignId": "b",
             "actionType": "CLAIM_BENEFIT", "claimStatus": "claimed"}]}
        camps = [c for c in qd.QoderCNProvider._campaigns(payload)
                 if c["action"] == "CLAIM_BENEFIT"]
        self.assertEqual(1, len(camps))
        self.assertEqual("CLAIMED", camps[0]["claim"])       # normalised upper
        # the key's date part must never be used to identify the cycle
        self.assertNotIn("2026-10-22", json.dumps(camps))

    def test_remaining_found_by_recursive_search(self):
        obj = {"data": {"tiers": [{"x": 1}, {"addOnQuota": {"remaining": 2750.0}}]}}
        self.assertEqual(2750.0, qd.QoderCNProvider._remaining(obj))
        self.assertIsNone(qd.QoderCNProvider._remaining({"nothing": {}}))

    def test_mask_never_emits_the_whole_secret(self):
        from keeper.log import mask
        out = mask("dt-" + "X" * 30)
        self.assertNotIn("XXXX", out)
        self.assertIn("len=", out)


class TestStateAndPaths(unittest.TestCase):
    def test_state_roundtrip_has_no_secrets(self):
        state.save("unit_test_provider", {"day": "2026-01-01", "state": "fresh",
                                          "message": "ok", "detail": {"balance": 1}})
        rec = state.load("unit_test_provider")
        self.assertEqual("fresh", rec["state"])
        blob = json.dumps(rec)
        for leak in ("Bearer", "accessToken", "refreshToken", "mmoat", "dt-"):
            self.assertNotIn(leak, blob)
        os.remove(state.path_for("unit_test_provider"))

    def test_local_day_uses_vendor_timezone(self):
        a = state.local_day(8)
        b = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))
        self.assertEqual(a, b)

    def test_config_env_override_beats_file(self):
        os.environ["DPK_NOTIFY_CHANNEL"] = "webhook"
        try:
            cfg = config.load()
            self.assertEqual("webhook", config.get(cfg, "notify.channel"))
        finally:
            del os.environ["DPK_NOTIFY_CHANNEL"]


class _Reader(object):
    def __init__(self, data):
        self._d = data

    def read(self, *a):
        return self._d

    def close(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
