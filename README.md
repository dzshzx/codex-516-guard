# codex-516-guard

[![PyPI](https://img.shields.io/pypi/v/codex-516-guard.svg)](https://pypi.org/project/codex-516-guard/)
[![Python](https://img.shields.io/pypi/pyversions/codex-516-guard.svg)](https://pypi.org/project/codex-516-guard/)
[![License: MIT](https://img.shields.io/pypi/l/codex-516-guard.svg)](https://github.com/dzshzx/codex-516-guard/blob/main/LICENSE)

安装：`uv tool install codex-516-guard` · PyPI：<https://pypi.org/project/codex-516-guard/>

> Local Responses proxy for OpenAI Codex CLI: detects the gpt-5.5 "516" reasoning-truncation
> fingerprint (`reasoning_tokens == 518*n - 2`), auto-continues the model's thinking, and folds
> all rounds into one response — **without changing `model_provider`**, so session grouping,
> remote compaction and remote-control stay intact. WebSocket-first: no
> "Falling back from WebSockets" retry noise.

自研本地 Responses 代理，缓解 Codex gpt-5.5 的「516 降智」：思考在
`reasoning_tokens == 518*n - 2`（516、1034、1552…）处被截断，答案质量骤降
（上游 issue：[openai/codex#30364](https://github.com/openai/codex/issues/30364)，无官方修复）。
本代理检测该指纹后自动让模型继续思考，并把多轮续写**折叠为单个下游响应**。

机制思路来自 [neteroster/CodexCont](https://github.com/neteroster/CodexCont)（MIT），
实现为全新代码。与其关键差异：

| | codex-516-guard | CodexCont |
| --- | --- | --- |
| Codex 侧接线 | 顶层 `openai_base_url`（**不新建 provider**） | 新建 `[model_providers]`（会话按 provider 分组被隐藏、remote-control 不可用、丢远程压缩） |
| 下游传输 | **WebSocket 第一传输**（完整实现 `responses_websockets` 协议）+ SSE 兜底 | 仅 SSE（codex 先试 ws → 405 → 每会话约 5 次重连告警后回退） |
| zstd 请求压缩（0.142.x 内置 provider 默认开） | 原生解压，无需改 codex 配置 | 需 `[features] enable_request_compression = false` |
| `GET /v1/models` 模型目录刷新 | `/v1/*` 透传 | 未代理（静默失败，靠本地缓存） |
| 续写方法 | commentary 法（`phase:"commentary"` 消息 + encrypted reasoning 重放） | commentary + tool_pair legacy + 跨轮 repair 等更多可配置项 |

## 原理

1. 上游每轮结束时读取 `usage.output_tokens_details.reasoning_tokens`，命中 `518n-2`（n∈[1,6]，最多续写 3 轮）即判定思考被截断；
2. 丢弃该轮的**暂定输出**（message / tool calls——它们基于被截断的思考），把该轮 reasoning items（含 `encrypted_content`）+ 一条 `Continue thinking...` 的 `phase:"commentary"` 助手消息追加进 input 重放，开下一轮；
3. 思考流实时透传给 agent，只有干净收尾那一轮的最终输出被放行；terminal 事件重建为单响应口径的 usage（input 取第 1 轮防止「假爆上下文」，reasoning 求和），真实累计成本记在 `metadata.proxy_billed_usage`。

## 安装

要求：[uv](https://docs.astral.sh/uv/)（自带 Python 管理）、Codex CLI（ChatGPT OAuth 登录，0.142.x 实测）。

```bash
uv tool install codex-516-guard          # 从 PyPI 安装
# 或直接从源码仓库：
# uv tool install git+https://github.com/dzshzx/codex-516-guard
```

uv 会建一个隔离环境并把可执行文件放进 uv 的 bin 目录（Unix/macOS 默认 `~/.local/bin`，
Windows 用 `where.exe codex-516-guard` 查实际路径；`uv tool update-shell` 可把该目录加进 PATH）。
之后：

```bash
codex-516-guard                          # 前台跑起（默认 127.0.0.1:8787）
codex-516-guard --port 8790 --log-level debug   # 可选：--host/--port/--upstream/--log-level/--strict-port
```

**端口避让**：默认端口（8787）被占用时，自动向后找第一个空闲端口（最多试 20 个）并在日志里打印实际
端口 + 对应的 `openai_base_url`。手动指定用 `--port`。若你已按固定端口给 Codex 接线（`openai_base_url`），
加 `--strict-port`——端口被占时**直接报错**而不是漂到别的端口（避免代理在跑、Codex 却连不上的隐性错配）。

升级 / 卸载：`uv tool upgrade codex-516-guard` / `uv tool uninstall codex-516-guard`。

Codex 侧接线——`~/.codex/config.toml` 顶层（必须在第一个 `[table]` 之前）加一行：

```toml
openai_base_url = "http://127.0.0.1:8787/v1"
```

这是覆盖内置 openai provider base_url 的**官方 config key**
（[#16719](https://github.com/openai/codex/issues/16719)；同名 `[model_providers.openai]`
覆盖被维护者拒绝，`OPENAI_BASE_URL` 环境变量已移除）。provider id 保持 `openai`，
因此会话历史分组、远程压缩、remote-control 均不受影响。

**关闭**：注释掉 `openai_base_url` 行 + 停掉代理进程。代理停止而 key 在位时，Codex 会因上游不可达报错。

## 开机自启动（可选，默认不开）

安装本身**不注册任何自启动**——是否开机自启完全由你决定。要开时一条命令，不想开就别运行它。

```bash
codex-516-guard install-service     # 注册并立即启动（当前平台）
codex-516-guard uninstall-service   # 撤销
```

`install-service` 按平台选「随用户登录启动、跑在用户上下文」的方式（而非系统级服务——系统服务跑在无用户环境的
session 里，够不到用户 profile 下的 uv 可执行文件与代理设置）：**Linux/WSL** → systemd user unit；
**macOS** → launchd LaunchAgent（`~/Library/LaunchAgents/`）。自定义端口等参数会一并写进自启动条目：
`codex-516-guard install-service --port 8790`。**Windows 例外**——`install-service` 只打印手动步骤、
不自动注册（行为杀软会把程序化自启持久化判为木马，详见下方 Windows 小节）。

下面是各平台**手动等价操作**（想自己管、或 `install-service` 在你的环境跑不动时用）。先用
`which codex-516-guard`（Unix/macOS）/ `where.exe codex-516-guard`（Windows）拿到绝对路径。

### Linux / WSL — systemd user unit

`install-service` 生成的等价 unit 见 `systemd/codex-516-guard.service.example`：

```bash
cp systemd/codex-516-guard.service.example ~/.config/systemd/user/codex-516-guard.service
systemctl --user daemon-reload && systemctl --user enable --now codex-516-guard
loginctl enable-linger   # 可选：无需登录也在开机时启动
```

### macOS — launchd LaunchAgent

macOS 用 launchd 管理后台任务。放在 `~/Library/LaunchAgents/` 的是 **LaunchAgent**，随**用户登录**启动、
跑在用户 GUI session 里（对回环代理正确的选择）；`/Library/LaunchDaemons/` 里的 LaunchDaemon 开机即起但无用户会话，本场景不适用。

把可执行文件绝对路径填进下面的 plist，存为 `~/Library/LaunchAgents/com.dzshzx.codex-516-guard.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>            <string>com.dzshzx.codex-516-guard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/codex-516-guard</string>
    </array>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <true/>
    <key>StandardOutPath</key>  <string>/tmp/codex-516-guard.log</string>
    <key>StandardErrorPath</key><string>/tmp/codex-516-guard.log</string>
</dict>
</plist>
```

注意：launchd 不读 shell 配置，`ProgramArguments` 必须是**绝对路径**；崩溃后 `KeepAlive` 会重启（10 秒节流）。
加载 / 停用（现代 `launchctl`，`load`/`unload` 已是 legacy）：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dzshzx.codex-516-guard.plist
launchctl enable   gui/$(id -u)/com.dzshzx.codex-516-guard
launchctl kickstart -k gui/$(id -u)/com.dzshzx.codex-516-guard   # 立即（重）启动
launchctl bootout  gui/$(id -u)/com.dzshzx.codex-516-guard        # 卸载
```

### Windows — 手动建启动项（工具不自动注册，出于杀软考虑）

`install-service` 在 Windows **不自动注册自启，只打印手动步骤**。原因：程序自动写入启动项
（Startup VBS / 注册表 Run 键 / 计划任务）再拉起隐藏进程，正是行为杀软判定木马持久化的特征——
实测 Kaspersky 的主动防御（PDM）会对执行该动作的 `python.exe` 报 `PDM:Trojan.Win32.Generic`。
**用户在受信任 UI 里自己建的启动项则被杀软信任。**

本包提供无窗口入口 `codex-516-guardw`（Windows GUI 子系统 exe，登录时无黑框）。手动自启：

1. `Win+R` 运行 `shell:startup` 打开启动文件夹；
2. 新建快捷方式，目标填 `where.exe codex-516-guardw` 得到的路径（需要自定义端口就在后面加 `--port 8790`）。

删除该快捷方式即取消自启。（`install-service` 在 Windows 会打印上述步骤和你的 exe 绝对路径。）

为什么不用系统 service：原生 service 要求实现 SCM 协议（`sc.exe` 直指控制台程序会 1053 超时），
且跑在 session 0/SYSTEM，够不到用户 profile 下 uv 装的 exe 与代理设置。

## 验证

```bash
curl -sS http://127.0.0.1:8787/healthz            # {"ok":true,...}
journalctl --user -u codex-516-guard -f | grep -E 'round|done'   # Linux/WSL；mac 看 plist 日志文件
```

命中折叠时的日志（实测样例，连环双 516 被击破、答案正确）：

```
round 1: in=21550 out=664 reason=516 total=22214 | n=1 buffered=['function_call'] -> continue
round 2: in=22078 out=652 reason=516 total=22730 | n=1 buffered=['function_call'] -> continue
round 3: in=22606 out=566 reason=291 total=23172 | n=None buffered=[...] -> clean
done: 3 round(s) | ... | status=completed stop=natural
```

## 开发

```bash
git clone https://github.com/dzshzx/codex-516-guard && cd codex-516-guard
uv sync
uv run python test_fold.py        # 折叠状态机自测，应输出 ALL PASS
uv run codex-516-guard            # 本地跑
```

发布走 PyPI Trusted Publishing（`.github/workflows/release.yml`，OIDC，无 token）：推 `v*` tag 即自动构建上传。

## 结构

- `guard/fold.py` — 指纹检测 + 折叠状态机（传输无关；`test_fold.py` 覆盖丢弃/放行、重编号、双口径 usage）
- `guard/server.py` — starlette 传输层：ws / SSE 下游、SSE 上游、zstd/gzip 请求解压、`/v1/*` 透传
- `guard/cli.py` — CLI 入口（`codex-516-guard`；仅监听回环；auth passthrough，不存储任何凭据）

## 安全与免责

- 代理只做 auth **passthrough**：转发 Codex 发来的 Authorization 头，不读取、不落盘任何凭据。
- 仅监听回环地址；不要暴露到非回环接口。
- 非官方项目，依赖上游未公开的行为（截断指纹、ws 帧格式），OpenAI 侧变更可能使其失效；使用风险自负。
- 续写会产生额外的真实 token 消耗（见 `metadata.proxy_billed_usage`），guard 以 n 窗口 + 3 轮上限约束。

## License

MIT（见 LICENSE；机制思路 credit neteroster/CodexCont）。
