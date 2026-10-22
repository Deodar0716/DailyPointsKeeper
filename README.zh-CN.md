# DailyPointsKeeper（中文）

把桌面 AI 客户端的"每日签到领积分"做成**纯 HTTP 脚本**：不点界面、不用模型、每天零
token 消耗。

这几个应用你本来就每天登录，它们每天发一点免费额度（"今天签到，得 100 Credits"）。
手动做是每天五次、你早晚会忘的操作；用界面自动化做则是纸糊的——锁屏就断、窗口挪位就
点错、客户端改版就全废。本项目改成：读客户端已经存在本机的登录态，直接调用那个按钮背后
的同一个接口。

```
C:\> python daily_points_keeper.py probe
[Qoder]        ok    本周期已领（act-YYYYMMDD-NNN）
[WorkBuddy]    ok    今日已签（活动=Buddy应用 累计=100 连签=1）
[MiniMax Code] ok    今日已领（第 1/7 天 400 分）
```

英文版见 [README.md](README.md)。机制细节（接口、枚举、踩过的坑）全部写在
[docs/mechanisms.md](docs/mechanisms.md)。

## 支持的服务

| provider key | 应用 | 每日额度 | 取凭据的方式 |
|---|---|---|---|
| `qoder_cn` | Qoder CN | 100 Credits | 客户端自己的 DPAPI 会话文件 `auth.v1.dat` |
| `workbuddy` | WorkBuddy（腾讯） | 积分 + 连签奖励 | 会话文件里加密字段，**由客户端自带的 Electron 解密** |
| `minimax_code` | MiniMax Code | 400–1000 积分 / 7 天一周期 | 明文 OAuth2 文件；脚本自行续期并写回 |

## 快速开始（Windows）

1. 给用户装 Python 3.9+，`git clone` 本仓库，双击 **`install.bat`**。它会自己找
   `pythonw.exe`、从模板生成 `config.json`、跑一次自检，并注册**一个**计划任务，一天
   五个时段（`10:05 12:05 15:05 18:05 21:30`）。不需要管理员权限。
2. 需要的话 `python -m pip install -r requirements.txt`——只有 Qoder 适配器要用
   （`cryptography`），另外两个是纯标准库。
3. 没装某个应用？把 `config.json` 的 `providers` 里那项删掉即可，其余照常。
4. 想要"领到了发我一条"：设 `notify.channel`（`hermes` / `webhook` / `none`）。

手动跑（排查时也用这几条）：

```
python daily_points_keeper.py doctor        # 本机就绪情况，不领取
python daily_points_keeper.py probe         # 只读：三个服务端各怎么说
python daily_points_keeper.py claim         # 计划任务实际执行的命令
python daily_points_keeper.py notify-test   # 只验通知通道
python -m unittest discover -s tests        # 29 条离线规则测试
```

## 判定逻辑

```
check()  →  done?        → ALREADY      静默，退出 0（不发消息）
            claimable?   → UNAVAILABLE  退出 4，只在当天最后时段才告警
            其余         → do_claim()   → FRESH   ✅ 发消息，退出 0
                                        → ALREADY ⏭ 静默，退出 0
                                        → ApiError / CredError → ❌ 退出 3 / 2，
                                                    同样只在最后时段告警
```

四条规则是设计的骨架，每条都对应一次"没这么做所以出事"：

* **服务端是唯一真值。** `var/state/*.json` 只写不读、绝不当跳过闸门。签到周期是上午
  10:00 刷新而不是 0 点，凌晨手跑一次写下 `checked: true`，当天 10:05 就被短路——整整
  一天额度静默丢失。
* **领取必须幂等，否则"已领"会被当成故障。** 三家都把"今天已经签过"返回成成功形态的
  应答；一天多个时段正是为了抵消偶发失败，所以"已领"不能算错误。
* **只有当天最后一个时段允许发失败告警。** 服务端发布当天活动可能晚于声称的刷新时间，
  早上 10:05 就报警等于每天早上狼来了。
* **通知失败绝不改变领取结果与退出码。** 没送达的进 `var/notify_pending.json`（上限
  8 条、超 3 天丢弃），下一次运行开头自动补发。最坏情况只是"到账了暂时没人告诉你"。

退出码是给计划任务用的契约（`LastTaskResult`）：`0` 正常 · `2` 凭据 · `3` 接口 ·
`4` 当天无可领。注意**计划任务不会因为退出码非 0 而重试**（`RestartCount` 只管启动失败
和被杀），重试靠的是多时段，而多跑几次不会重复领。

## 通知

只看 `config.json` 里的 `notify.channel`：

* `hermes` —— 调本机 [Hermes](https://github.com/NousResearch/hermes-agent) 机器人网关
  的 `hermes send --to <目标> --json <文本>`，脚本自己不接触消息密钥。目标按顺序试
  （主→备）。微信侧对"机器人主动推送"有互动窗口限制，十几小时没入站消息就会被拒，所以
  建议再配一个备用目标（QQ 侧实测没有这个限制）。
* `webhook` —— 一个 POST 地址，按域名自动成形：企业微信群机器人 / 钉钉机器人 /
  PushPlus / Server酱（`*.ftqq.com`），其余走通用 JSON。
* `none` —— 不发通知，并直接记为"已送达"，免得队列在故意静音的机器上越积越多。

两条成功推送之间由 `Notifier` 内部按 `notify.gap_seconds`（35 秒）自动隔开，计时点是
**上一次真的发出去**。所以 provider 之间不需要 `sleep`：没有任何新到账的日子，整轮跑只
花几秒。

## 安全与隐私

* **密钥不出本机。** 令牌每次现读、只用于一次 HTTPS 请求，绝不进日志、状态文件、
  argv 或消息正文。日志里唯一的呈现方式是 `mask()` → `<len=60 head=mmoat_…>`。
* **`config.json` 已被 .gitignore 排除**，这是必须的：里面有聊天目标 ID 和本机路径。
  仓库只发布带占位符的 `config.example.json`。
* 凭据只从当前 Windows 用户自己的配置目录读，且只有创建它的客户端能解。WorkBuddy 这条
  脚本压根不提取密钥，而是把客户端自带的 Electron 当解密器用。
* 有一个 provider 会写不属于自己的文件：MiniMax 的 `auth.json`。原因是这家每次续期都
  轮换 `refresh_token`，**不写回就等于把桌面客户端踢下线**。写回时会抢客户端自己的锁、
  保持它原有格式与 `generation`、并在失败时回滚。改这段之前先读
  [docs/mechanisms.md](docs/mechanisms.md)。
* 全部请求都开启证书校验，仓库里没有 `verify=False`。
* 频控上靠"一天几个时段"分散尝试，不建号、不绕付费、不刷别人账号。各家的服务条款请自行
  确认，见 [NOTICE](NOTICE)。

## 加一个新服务

一个文件、两个方法：`check()` 只读、`do_claim()` 幂等。判定规则、退出码、通知文案、
日志轮转、补发队列都已经在框架里做完。带 60 行示例和结果对照表：
[docs/adding-a-provider.md](docs/adding-a-provider.md)。

## 目录结构

```
daily_points_keeper.py        计划任务入口
keeper/
  config.py                   默认值 < config.json < DPK_* 环境变量
  result.py                   Result + 退出码 + 汇总优先级
  log.py                      UTF-8 安全日志（pythonw 没有控制台）
  notify.py                   hermes / webhook / none + 待发队列 + 节流
  state.py                    每服务状态记录——只作记录，绝当闸门
  runner.py                   判定规则只写在这里
  discover.py                 不写死盘符地找已安装的客户端
  providers/                  base.py 契约 + 每家一个文件
tools/wb_desktop_session.cjs  第三方 MIT 助手（见 NOTICE）
tests/test_rules.py           上面那些规则的 29 条离线测试
docs/                         mechanisms · adding-a-provider · troubleshooting
SKILL.md                      同一套操作说明，写给 AI 代理执行
var/                          日志、状态、队列（已 gitignore，运行时才创建）
```

## 局限

* 以 Windows 为先。`qoder_cn` 目前只能 Windows（DPAPI）；另外两家要移植的话需要适配各自
  客户端在本机的存放布局（macOS 钥匙串 / 不同的 profile 目录）。
* 计划任务只在"该用户处于登录态"时运行，因为凭据属于那个用户的会话。但**屏幕锁着照样
  能跑**，这正是不用界面自动化的理由。
* 活动会结束。WorkBuddy 当前就带着截止日期；MiniMax 的令牌由客户端每小时续。服务端改动
  接口时，provider 会给出明确的凭据/接口失败信息而不是猜——见
  [docs/troubleshooting.md](docs/troubleshooting.md)。
* 以上是 2026 年 10 月对客户端 3.1.4 / 5.7.x / 0.4.x 的实测结论，不代表这些接口一直存在。

## 许可

[MIT](LICENSE) · 第三方来源与使用声明见 [NOTICE](NOTICE)
