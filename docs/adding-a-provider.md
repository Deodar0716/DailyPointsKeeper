# Adding a fourth service

A provider is one file with two methods. Everything that is easy to get wrong —
already-claimed silence, "only alert at the end of the day", exit codes, retry
queue, log rotation, the record-not-gate rule — is done once in
`keeper/runner.py`.

## 1. The file

`keeper/providers/example_app.py`:

```python
# -*- coding: utf-8 -*-
"""ExampleApp — daily points. Endpoints observed 2026-01."""

import json, os, ssl, urllib.request, urllib.error

from ..result import ALREADY, FRESH
from .base import ApiError, CredError, Provider, Snapshot


class ExampleAppProvider(Provider):
    key = "example_app"                 # must match the name in providers/__init__.py
    label = "ExampleApp"
    unit = "points"

    def __init__(self, cfg, log):
        super().__init__(cfg, log)
        self.ssl = ssl.create_default_context()

    # ---- read-only: what does the server say right now? -------------
    def check(self):
        data = self._get("/api/points/status")
        if data.get("checked_in_today"):
            return Snapshot(done=True, claimable=False,
                            summary="今日已签（累计 %s）" % data.get("total"))
        if not data.get("available"):
            return Snapshot(done=False, claimable=False, summary="今天没有档位")
        return Snapshot(done=False, claimable=True,
                        summary="可领 +%s" % data.get("today_amount"),
                        detail={"amount": data.get("today_amount")})

    # ---- perform the claim. must be safe to call twice. ------------
    def do_claim(self):
        r = self._post("/api/points/claim", {})
        if r.get("code") == 1000:                        # vendor's "already"
            return Snapshot(outcome=ALREADY, summary="服务端判定已签（幂等）")
        if r.get("code") != 0:
            raise ApiError("claim code=%s %s" % (r.get("code"), json.dumps(r)[:120]))
        after = self._get("/api/points/status")          # verify, never trust one code
        return Snapshot(outcome=FRESH, gained=after.get("total"),
                        summary="已领到手 +%s" % r.get("amount", ""),
                        detail={"total": after.get("total")})

    # ---- transport helpers (raise the two typed errors) -------------
    def _token(self):
        path = os.path.expanduser("~/.exampleapp/token")
        if not os.path.isfile(path):
            raise CredError("请先登录 ExampleApp（找不到 %s）" % path)
        return open(path).read().strip()

    def _get(self, p):
        return self._call(p, None)

    def _post(self, p, body):
        return self._call(p, json.dumps(body).encode())

    def _call(self, p, data):
        req = urllib.request.Request("https://api.example.com" + p, data=data,
                                     method="POST" if data else "GET",
                                     headers={"Authorization": "Bearer " + self._token(),
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25, context=self.ssl) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)      # 4xx bodies often carry the real code
            except Exception:
                raise ApiError("HTTP %s %s" % (e.code, body[:120]))
            if e.code in (401, 403):
                raise CredError("HTTP 401/403，令牌失效")
            return parsed
        except Exception as e:
            raise ApiError(repr(e))
```

## 2. Two rules the methods must respect

**`check()` is read-only.** It answers three questions and nothing else:
`done` (today already claimed), `claimable` (a claim would do something now),
`summary` (one human sentence). The runner turns those into
ALREADY / UNAVAILABLE / "call do_claim()" — silently, no notification, exit 0/4.

**`do_claim()` must be idempotent, and must say which of the three happened** by
setting `outcome`:

| outcome          | meaning                        | runner does                       |
|------------------|--------------------------------|-----------------------------------|
| `FRESH`          | points landed this call        | ✅ notification, exit 0           |
| `ALREADY`        | server says already claimed    | silence, exit 0                   |
| `UNAVAILABLE`    | nothing to claim now           | silence (last-slot alert only), 4 |
| raise `ApiError` | transport/business failure     | ❌ only at the last slot, exit 3  |
| raise `CredError`| login/credential problem       | ❌ only at the last slot, exit 2  |

Do not report "already claimed" as an error: it is the normal result of the
second, third and fourth slot of the same day.

## 3. Register it

1. add the class to the table in `keeper/providers/__init__.py`:

   ```python
   _MODULES = {"example_app": ("example_app", "ExampleAppProvider"), ...}
   ```

2. add `"example_app"` to `providers` in `config.json`.
3. `python daily_points_keeper.py doctor` then `... probe` then `... claim`.
4. add a case to `tests/test_rules.py` for whatever judgement call the vendor
   makes that is surprising (that is the part worth pinning).

## 4. Optional: `doctor()`

Return `[(name, ok, note), ...]` for local readiness — credential file present,
optional dependency installed, client found. It must not need the network. This
is what makes a fresh machine explain itself instead of failing at 10:05.

## 5. What never to do

* Never write the token/secret into a log, a state file, argv, or the
  notification body. Use `keeper.log.mask()` if you need to show a credential's
  *shape*.
* Never read `var/state/*.json` to decide whether to skip a run. That rule
  exists because a 10:00-rollover vendor plus a midnight-keyed flag silently lost
  a day. The server is the only truth.
* Never add fixed `time.sleep()` between providers to protect the notification
  channel — `Notifier` throttles itself from the last *successful* send.
* Never assume a GUI is available. If a step needs clicking, it is out of scope
  for this project (see README, "Why not UI automation").
