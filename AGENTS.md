# Codex 项目契约

## 代理不变量

- `codexcomp` 是 Codex CLI 与上游 Responses API 之间的本地环回代理，地址为 `127.0.0.1:8787`。通过顶层 `openai_base_url` key 配置它，绝不用 `[model_providers]` 条目。
- bind 只能保持环回。按原样转发 `Authorization`；绝不检查、记录或持久化它。
- 干净的上游轮次必须逐字节透传。`fold()` 拥有终止事件：上游 EOF、流错误和续写打开错误变为 `response.incomplete`；被拒绝的首轮变为 `response.failed`。绝不静默丢失输出或伪造完成响应。

## 聚焦验证与 eval

- 改 `fold.py` 前运行 `uv run python test_fold.py`。改 `server.py` 的 WebSocket 路径前运行 `uv run python test_ws.py`。二者都是以 `ALL PASS` 结束的 assert 脚本；本仓没有 pytest、lint 或 typecheck 套件。
- 仅在明确有意时运行 `uv run codexcomp-eval` 或 `uv run codexcomp-sudoku-eval`：每次都会调用 Codex 并消耗真实 tokens 与 quota。

## 文档与发布

- 对用户可见的行为，让 `README.md` 与 `README.zh-CN.md` 保持一致。保留 neteroster/CodexCont 的机制致谢，并让 `LICENSE` 保持纯 MIT 文本。
- 发布时依次步进包版本、提交并推送 `master`、推送匹配的带注解 `v*` tag、等待发布 workflow 与 PyPI，然后创建 GitHub Release。systemd unit 绝不自行更新：只有明确存在活跃的本地 uv-tool/service 部署时，才以 `uv tool upgrade codexcomp` 和 `systemctl --user restart codexcomp` 收尾；否则跳过这些命令，不能臆测存在部署。
