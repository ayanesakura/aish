# aish — AI Studio WebShell 终端客户端

在 mac 终端里直接接入 AI Studio 训练 pod 的 shell（平台网页 WebShell 背后的 WebSocket），效果类似 `ssh -t`：
原始 tty 透传、窗口大小同步、保活、令牌自动获取。

## 准备
- 需要 `uv`（脚本是 `uv run --script`，依赖 `websockets` 会自动安装）。
- 凭据：浏览器登录 AI Studio，把 Cookie `ais_token` 的**值**写入 `~/.config/ais-webshell/ais_token`（`chmod 600`）。
  可用环境变量 `AIS_TOKEN_FILE` 指定别的路径。cookie 失效时 aish 会提示重新登录并更新该文件。
  获取方式：Chrome 打开 AI Studio → F12 → Application → Cookies → `https://ais.mlp.shopee.io` → 复制 `ais_token` 的 Value，然后：
  ```bash
  mkdir -p ~/.config/ais-webshell
  pbpaste > ~/.config/ais-webshell/ais_token && chmod 600 ~/.config/ais-webshell/ais_token
  ```

## 用法
```bash
U='https://ais.mlp.shopee.io/projects/modeling/experiment/web-shell?containerName=main&experimentId=...&podName=...&projectId=...&runId=...'

aish "$U"                          # 交互模式（类似 ssh -t）
aish "$U" -- tmux new -A -s cc     # 连上后自动输入该命令，之后仍为交互模式
aish "$U" --exec 'nvidia-smi'      # 非交互：打印输出，按远端退出码退出（--timeout 默认 30s，超时退出码 124）
aish --project P --experiment E --run R --pod POD [--container main]   # 不用 URL，显式给参数
aish "$U" -v ...                   # 调试输出（URL 里的 token 会打码）
```

交互模式：
- 行首 `~.` 断开连接（只断开，不向远端发送任何内容）；`~~` 发送一个 `~`；`~?` 显示帮助。
- 在最外层 shell 里输入 `exit` / `logout`（或空行按 `^D`）后，aish 会识别出远端 shell 已退出并自动结束（退出码 0）。
- 连接意外断开时，aish 恢复终端、打印一行原因后退出（退出码 1），不会自动重连。

## ⚠️ 断开 ≠ 退出
实测发现，**WebSocket 断开时网关不会回收远端 shell**（远端 shell 退出时网关也不会关闭 WebSocket）。
所以 `~.`、直接关掉终端窗口、网络断线，都会在 pod 上留下一个游离的 bash。建议：
- 不用时在远端输入 `exit` 退出；
- 长时间的工作放在 tmux 里（`aish "$U" -- tmux new -A -s work`），断线后再 attach。
`--exec` 模式结束时会自动让远端 shell `exit`。

## 实现说明
- 换令牌：`GET /api/experiment/v1/.../webshellUrl`（带 cookie），每次连接都会拿到新的 session 和 token（JWT，约 1 小时有效）。
- 连接：把 `/webshell/terminal` 换成 `wss://.../webshell/opsws`，查询串原样保留，再追加 `&cols=&rows=`，握手时带 cookie。
- 输入原样以文本帧发送；输出可能是文本帧或二进制帧，直接按字节写到 stdout。
- 保活节奏照抄网页的 heartCheck：连接后约 38s 发第一个空帧，之后每 28s 发一个。
- 窗口大小：连接后（收到首个输出或 2s 后）调用一次 `opsresize`，之后每次 SIGWINCH 再调用。
- 本地终端属性在正常退出、`~.`、异常和 SIGTERM/SIGHUP/SIGINT 路径上都会恢复。

## 已知局限 / 未验证
- **token 过期（约 1 小时）后，已建立的连接是否还能继续用：未验证。** 网页脚本里没有任何 token 刷新或重连逻辑，
  token 只在握手 URL 中出现，推测连接建立后不会再校验，但没有做长时间实测。
- 远端 shell 退出的自动识别是启发式的：只认「输入 `exit`/`logout`/^D 回车、输出以 `exit`/`logout` 结尾、且 1.5s 内没有出现新提示符」。
  用历史命令（↑）调出 exit、`exec` 掉 shell 等情况识别不了，这时请按 `~.`。反过来，在 `cat` 这类会把输入原样回显的程序里输入 `exit` 回车，可能被误判为 shell 已退出。
- `--exec` 用 `eval` 包住命令，用随机结束标记判断命令何时结束；命令如果自己执行 `exit`，会得到退出码 255。
- pod 如果用了 hostNetwork，`hostname` 显示的是节点名，不是 pod 名（本次测试的 pod 就是这样）。
- `AIS_BASE_URL` 环境变量可以覆盖平台地址，仅用于本地 mock 测试。

## 开发测试（不连生产 pod）
`dev/mock_server.py` 在本机模拟 webshellUrl / opsws / opsresize（后端是本机 bash pty，二进制帧故意拆 UTF-8）；
`dev/pty_test.py` 用 pty 驱动 aish 测交互模式（提示符、回显、窗口大小、`~.`、终端属性恢复）。
```bash
dev/mock_server.py 18765 /tmp/mock.log &          # MOCK_KEEP_OPEN=1 可模拟“shell 退出后不关 WS”的真实网关
printf 'mockcookie123456' > /tmp/mock_cookie
export AIS_BASE_URL=http://127.0.0.1:18765 AIS_TOKEN_FILE=/tmp/mock_cookie
U='https://x/y?containerName=main&experimentId=1&podName=p&projectId=2&runId=3'
./aish "$U" --exec 'echo hi'
python3 dev/pty_test.py full x -- ./aish "$U"
```
