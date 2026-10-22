---
name: daily-points-keeper
description: 安装、验证或排障 DailyPointsKeeper —— 用纯 HTTP 脚本自动领取桌面 AI 客户端（Qoder CN、WorkBuddy、MiniMax Code）的每日签到额度。当用户说"每天的积分没领到""帮我把签到做成自动任务""为什么没收到签到消息""计划任务报了 2/3/4""加一个 XX 客户端的每日签到""脚本能不能换台机器跑"等场景时使用。禁止用界面自动化替代它。
when_to_use: 每日签到/积分领取的搭建、验收、排障，或为第四个服务写 provider
---

# DailyPointsKeeper 运维手册

领每日签到这件事只有一条路是稳的：**读客户端已存在本机的登录态，调它按钮背后的同一个接
口**。界面自动化（点鼠标、找按钮）在锁屏、窗口移位、客户端改版三种情况下必死，本仓库存
在的理由就是不再走那条路。所以先按本文档判断"是接口问题还是通知问题"，不要一上手就想到
点界面。

命令都在仓库根目录执行，三条只读命令随时可跑：

```
python daily_points_keeper.py doctor    # 本机就绪情况（不联网领取）
python daily_points_keeper.py probe     # 只读：各服务端今天怎么说，绝不领取、绝不通知
python daily_points_keeper.py claim     # 计划任务实际执行的那条
```

**以脚本输出为准**，文档里的版本号和接口形态是 2026-10 的快照。

## 硬规矩（违反过的都写在这）

1. **不许把"今天已领"当失败。** 三家都把幂等重放返回成成功形态（Qoder `replayed:true`、
   WorkBuddy `HTTP 400 + code 10001`、MiniMax `claim_result=2`）。一天跑 5 个时段是设计
   的一部分，多跑不会重复领。
2. **状态文件只写不读。** 判定一律现问服务端。签到周期可能上午才刷新（Qoder 是 10:00
   UTC+8），凌晨写下的 `checked:true` 会让当天整天静默漏领——这条已经真实发生过。
3. **失败告警只在当天最后一个时段发。** 服务端发布当天活动可能晚于声称的时间，白天看到
   "没有活动"多半是还没发，退出码 4 是正常等待而不是故障。
4. **通知失败不许改动领取结果。** 未送达进 `var/notify_pending.json`，下次运行开头补发。
   抱怨"没收到消息"时先分清：`probe` 说已领 ⇒ 领取成功，只是投递问题。
5. **令牌不进任何持久处。** 日志只能出现 `mask()` 的形状（`<len=60 head=mmoat_…>`）；
   不许把 token 打到聊天、状态文件、argv 或通知正文里。`config.json` 含聊天 ID 与本机
   路径，**永远不许提交**，对外发日志前先检查里面没有完整 `eyJ…`/`dt-`/`mmoat_`。
6. **provider 之间不加固定 `sleep`。** 通知节流由 `Notifier` 按"上一次真的发出去了"自动
   处理（`notify.gap_seconds=35`）；写死等待只会让无事的日子白等一分钟。
7. **MiniMax 的 `auth.json` 要写就必须带锁、带格式、带回滚。** 那家每次续期都轮换
   `refresh_token`，不写回会把用户的桌面客户端踢下线；写回时抢 `auth.lock.lock` 目录、
   保持 `indent=2`+尾换行、保留 `generation`（改了会被当未登录）。
8. **不许关证书校验。** 全仓库都该是 `ssl.create_default_context()`，没有 `verify=False`。

## 判活与排障顺序

```
1) probe        → 服务端真值（已领/可领/无档位/凭据坏/接口坏）
2) var/logs/keeper.log + var/logs/<provider>.log
3) schtasks /Query /TN DailyPointsKeeper /V /FO LIST   （Last Task Result / Run As User / Next Run Time）
4) var/notify_pending.json + var/logs/notify.log        （只有这一步才涉及"没收到消息"）
```

退出码 → 结论：

| LastTaskResult | 含义 | 下一步 |
|---|---|---|
| `0` | 已领或本来就已领 | 若用户说"没消息"：看第 4 步，别再查领取 |
| `2` | 凭据问题（某一家没登录/DPAPI 不可用/缺 cryptography） | 让用户打开对应客户端登录一次；确认任务是以**该交互用户**跑的（LogonType=Interactive） |
| `3` | 接口/网络 | 对照 `docs/mechanisms.md` 复核该家的参数与状态码 |
| `4` | 当天无可领档位 | 可能发布晚了；确认最后一个时段的 `alert.last_slot_minutes` 是否设在最后一个时段 |
| `267009` (0x41301) | 正在运行 | 等它结束，别重复启动 |

**计划任务不会因为退出码非 0 而重试**（`RestartCount`/`RestartInterval` 只管"启动失败"和
"被异常终止"）。要重试就加时段，别去调 RestartCount——这条也踩过。

## 装机（新机器）

1. `git clone` → 双击 `install.bat`（无管理员权限）。它会选解释器、生成 `config.json`、
   跑 `doctor`、注册五个时段。
2. `python -m pip install -r requirements.txt`（只有 Qoder 需要 `cryptography`）。
   ⚠ 一定用**任务实际使用的那个解释器**装；PATH 上的 `python` 可能指向别人的 venv。
3. 只跑用户实际装了的那几家：`config.json` 里删掉多余 provider。
4. 通知：`hermes`（本机机器人网关，脚本不碰消息密钥）/ `webhook`（企业微信、钉钉、
   PushPlus、Server酱）/ `none`。目标 ID 只写在 `config.json`。
5. 验收三步全过再交差：`doctor` → `probe` → `schtasks /Run /TN DailyPointsKeeper` 后
   `LastTaskResult=0` 且 `keeper.log` 里有本轮各 provider 的结论行。

卸载：`uninstall.bat`（或 `register_task.ps1 -RemoveOnly`）。日志/状态留在 `var\`。

## 加第四个服务

见 `docs/adding-a-provider.md`：一个文件、`check()` + `do_claim()` 两个方法，返回
`Snapshot(outcome=FRESH|ALREADY|UNAVAILABLE)`，异常只抛 `CredError` / `ApiError`。判定
规则、文案、退出码、队列都在框架里，provider 里**不要**重复实现。动手前先在真机上确认
三件事：接口路径与成功判据（业务码，不是 HTTP 码）、幂等语义、本机凭据的可读性。逆向顺
序建议：客户端自己的请求日志（如 `%APPDATA%\MiniMax\logs\api-*.log` 一行就是一条完整请
求，含全部 query 参数）→ `app.asar` 里的渲染层代码 → 最后才是抓包。

## 参考文件

* `docs/mechanisms.md` —— 三家的接口、枚举、必带参数、密钥布局，以及每条坑的成因
* `docs/adding-a-provider.md` —— 60 行示例与 outcome 对照表
* `docs/troubleshooting.md` —— 按症状查表
* `tests/test_rules.py` —— 29 条离线规则测试（改判据必须先让它过）
* `NOTICE` —— 第三方代码来源与使用声明
