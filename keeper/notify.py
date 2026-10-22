# -*- coding: utf-8 -*-
"""Notifications, with a retry queue.

Two channels, chosen by `notify.channel`:

* `hermes`  — runs `hermes send --to <target> --json <text>`. Hermes is a local
  bot gateway (WeChat / QQ) whose `send` subcommand is made for scripts: no LLM,
  no agent loop, exit code 0/1/2. The script never touches the bot's keys.
  `notify.hermes.targets` is tried **in order**: first line = primary, the rest
  = fallbacks. Two channels exist because the WeChat side has an
  "interaction window": a bot-initiated push can be refused with
  `sendmessage rate limited; cooldown active for 30.0s` until the user sends one
  inbound message. That is rate control, not session expiry, so no amount of
  in-process sleeping fixes it — you switch channel, and if all fail you queue.
* `webhook` — one POST URL. Payload is auto-shaped by hostname for
  WeCom group bot / DingTalk bot / PushPlus / Server酱, else generic JSON.
* `none`    — notify() reports success and does nothing, so the queue never
  fills up on a machine that deliberately wants no notifications.

Reliability contract (do not weaken it):
1. notification failure **never** changes a claim result or the exit code;
2. undelivered messages go to `var/notify_pending.json` (cap `pending_max`,
   dropped after `pending_keep_days`) and are re-flushed at the start of the
   next run, so a claim that succeeded silently is still reported eventually;
3. every failure writes a greppable line containing `NOT-DELIVERED`.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .log import Log

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_SSL = __import__("ssl").create_default_context()

_SYM = {"fresh": "✅", "already": "⏭️", "unavailable": "⏭️", "error": "❌", "cred": "❌"}


class Notifier(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = Log("notify")
        self.channel = (config.get(cfg, "notify.channel", "none") or "none").lower()
        self.gap = int(config.get(cfg, "notify.gap_seconds", 35) or 35)
        self.max = int(config.get(cfg, "notify.pending_max", 8) or 8)
        self.keep_days = int(config.get(cfg, "notify.pending_keep_days", 3) or 3)
        self.pending_path = os.path.join(config.var_dir(), "notify_pending.json")

    # ---------------- public ----------------

    def send(self, title, content):
        """Deliver or enqueue. Always returns, never raises."""
        if self.channel == "none":
            self.log("[notify] channel=none，跳过投递：%s" % title)
            return True
        if self._deliver(title, content):
            return True
        items = self._load()
        items.append({"ts": time.strftime("%Y-%m-%d %H:%M"),
                      "title": title, "content": content})
        self._save(items)
        self.log("[notify] ⚠ NOT-DELIVERED，已入队等下次补发：%s" % title)
        return False

    def flush(self):
        """Re-send whatever a previous run could not deliver."""
        if self.channel == "none":
            return
        items = self._load()
        if not items:
            return
        cutoff = time.time() - self.keep_days * 86400
        left = []
        for it in items:
            try:
                fresh = time.mktime(time.strptime(it.get("ts", ""), "%Y-%m-%d %H:%M")) >= cutoff
            except Exception:
                fresh = True
            if not fresh:
                self.log("[notify] 丢弃过期未送达通知：%s" % it.get("title"))
                continue
            if not self._deliver(it.get("title", ""), it.get("content", "")):
                left.append(it)
        if len(left) != len(items):
            self._save(left)

    def describe(self):
        if self.channel == "hermes":
            targets = config.get(self.cfg, "notify.hermes.targets", []) or []
            return "hermes → %d 通道（%s）" % (
                len(targets), ", ".join(str(t).split(":")[0] for t in targets) or "未配置")
        if self.channel == "webhook":
            url = config.get(self.cfg, "notify.webhook.url", "") or ""
            host = urllib.parse.urlparse(url).hostname or "未配置"
            return "webhook → %s" % host
        return "none（不发通知）"

    # ---------------- hermes ----------------

    def _hermes_exe(self):
        cand = [os.environ.get("HERMES_EXE", ""),
                config.get(self.cfg, "notify.hermes.exe", "") or ""]
        for c in cand:
            if c and os.path.isfile(c):
                return c
        return shutil.which("hermes") or ""

    def _via_hermes(self, body):
        exe = self._hermes_exe()
        if not exe:
            self.log("[notify] 找不到 hermes.exe（配 notify.hermes.exe 或装 hermes）")
            return False
        targets = config.get(self.cfg, "notify.hermes.targets", []) or []
        if not targets:
            self.log("[notify] notify.hermes.targets 为空")
            return False
        for target in targets:
            try:
                p = subprocess.run([exe, "send", "--to", str(target), "--json", body],
                                   capture_output=True, text=True, timeout=25,
                                   encoding="utf-8", errors="replace",
                                   creationflags=_CREATE_NO_WINDOW)
                rc, out, err = p.returncode, (p.stdout or "").strip(), (p.stderr or "")
            except Exception as e:
                self.log("[notify] hermes 调用异常 %r" % e)
                continue
            # Read the error from the JSON's `error` key ONLY. A success payload
            # has no such key; falling back to `out` there used to mark
            # delivered messages as failures and jam the queue forever.
            why = ""
            try:
                j = json.loads(out)
                why = str(j.get("error") or "") if isinstance(j, dict) else ""
            except Exception:
                why = (out or err)[:180]
            if rc == 0 and not why:
                self.log("[notify] 已通过 hermes(%s) 送达" % str(target).split(":")[0])
                return True
            self.log("[notify] hermes(%s) 失败 rc=%s %s"
                     % (str(target).split(":")[0], rc, why))
        return False

    # ---------------- webhook ----------------

    def _webhook_payload(self, title, text):
        url = config.get(self.cfg, "notify.webhook.url", "") or ""
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        headers = {"Content-Type": "application/json"}
        if not url:
            return None, None, None
        if "qyapi.weixin.qq.com" in host or "oapi.dingtalk.com" in host:
            return url, headers, json.dumps(
                {"msgtype": "text", "text": {"content": "%s\n%s" % (title, text)}}).encode()
        if "pushplus" in host:
            parts = urllib.parse.urlparse(url)
            seg = [s for s in parts.path.split("/") if s]
            token = seg[1] if len(seg) >= 2 and seg[0].lower() == "send" else \
                urllib.parse.parse_qs(parts.query).get("token", [""])[0]
            return url, headers, json.dumps({"token": token, "title": title,
                                             "content": text, "template": "html"}).encode()
        if "ftqq.com" in host:                       # Server酱 wants a form
            return url, {"Content-Type": "application/x-www-form-urlencoded"}, \
                urllib.parse.urlencode({"title": title, "desp": text}).encode()
        return url, headers, json.dumps({"title": title, "text": text}).encode()

    def _via_webhook(self, title, body):
        url, headers, data = self._webhook_payload(title, body)
        if not url:
            self.log("[notify] notify.webhook.url 未配置")
            return False
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=15, context=_SSL) as r:
                raw = r.read().decode("utf-8", "replace")
            self.log("[notify] 已通过 webhook 送达 HTTP %s %s" % (r.status, raw[:120]))
            return True
        except Exception as e:
            self.log("[notify] webhook 失败 %r" % e)
            return False

    # ---------------- plumbing ----------------

    def _throttle(self):
        """Rate windows are per *successful* push, so only a delivered message
        arms the wait. Providers therefore need no `sleep` between them: if
        nothing is worth notifying, the whole run costs seconds instead of a
        minute of fixed gaps.
        """
        last = getattr(self, "_last_sent", 0)
        wait = self.gap - (time.time() - last)
        if last and 0 < wait <= self.gap:
            self.log("[notify] 距上次送达不足 %d 秒，等 %.0f 秒再发下一条" % (self.gap, wait))
            time.sleep(wait)

    def _deliver(self, title, content):
        body = "%s\n%s" % (title, content)
        self._throttle()
        if self.channel == "hermes":
            ok = self._via_hermes(body)
        elif self.channel == "webhook":
            ok = self._via_webhook(title, body)
        else:
            return False
        if ok:
            self._last_sent = time.time()
        return ok

    def _load(self):
        try:
            with open(self.pending_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def _save(self, items):
        try:
            with open(self.pending_path, "w", encoding="utf-8") as f:
                json.dump(items[-self.max:], f, ensure_ascii=False, indent=1)
        except Exception as e:
            self.log("[notify] 队列写入失败 %r" % e)


def render(title, lines):
    """Small helper so providers format one message the same way everywhere."""
    return title, "\n".join(lines)
