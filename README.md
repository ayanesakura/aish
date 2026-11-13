# aish — AI Studio WebShell 终端客户端

在 mac 终端里直接接入 AI Studio 训练 pod 的 shell，效果类似 `ssh -t` / `herdr --remote`。
mac 和 pod 之间没有直连网络，aish 走的是平台网页 WebShell 背后的那条 WebSocket。

- **会话常驻**：默认接入 pod 上的 tmux 会话。关终端、断网、合盖都不影响 pod 上正在跑的东西（比如 Claude Code）。
- **自动重连**：断线后自动重新换令牌，接回同一个 tmux 会话。会主动探测连接是否还活着（WebSocket ping），网络断开后约 25 秒内就能发现。
- **按名字连接**：`aish add` 保存一次，以后只需要 `aish gpu`。
- **cookie 自动获取**：直接读取本机 Chrome 里的 `ais_token`，过期了也会自动刷新。

## 安装
需要 [uv](https://docs.astral.sh/uv/)。依赖（websockets、cryptography）在第一次运行时自动安装。
```bash
git clone https://github.com/ayanesakura/aish.git ~/aish
echo 'alias aish=~/aish/aish' >> ~/.zshrc && source ~/.zshrc
```

## 日常用法
```bash
# 1. 保存机器（只需一次）：URL 就是平台上 pod 的 WebShell 页面地址
aish add gpu 'https://ais.mlp.shopee.io/projects/modeling/experiment/web-shell?containerName=main&experimentId=...&podName=...&projectId=...&runId=...'

# 2. 接入：进入 pod 上的 tmux 会话 main。第一次进来后启动 claude；以后每次都回到同一个画面
aish gpu

# 3. 离开：Ctrl-b d（tmux detach）或行首 ~.，pod 上的会话继续运行
```
- 断网或合盖后：aish 会提示「连接断开；N s 后重连」，然后自动接回。不想等的话，行首按 `~.` 放弃重连。
- 在另一台电脑或另一个终端运行 `aish gpu`：会接管这个会话，旧的客户端自动退出。
- 在 tmux 里 `exit` 掉最后一个 shell：tmux 会话结束，aish 随之退出。

其他用法：
```bash
aish gpu --exec 'nvidia-smi'       # 非交互：打印输出，按远端退出码退出（--timeout 默认 30s，超时退出码 124）
aish gpu --tmux work               # 临时用另一个 tmux 会话名
aish gpu --no-tmux                 # 普通 shell，不用 tmux（断线不会自动重连）
aish gpu -- htop                   # 普通 shell，连上后自动输入命令
aish ls / aish rm gpu              # 列出 / 删除保存的机器（~/.config/ais-webshell/hosts.json）
aish add gpu2 '<URL>' --tmux cc    # 保存时指定 tmux 会话名；--no-tmux 表示保存为普通 shell
aish '<URL>'                       # 不保存，直接用 URL（默认是普通 shell，加 --tmux NAME 用 tmux）
aish login                         # 手动从 Chrome 刷新 cookie
```

## cookie
- aish 从 `~/.config/ais-webshell/ais_token` 读取 cookie（600 权限，只存值；可以用 `AIS_TOKEN_FILE` 指定别的路径）。
- 文件不存在，或者 cookie 被平台拒绝时，aish 会自动从本机 Chrome（也支持 Edge / Brave / Arc / Chromium）读取 `ais_token` 并写回这个文件。
  - 第一次读取时 macOS 会弹出钥匙串授权（「Chrome Safe Storage」），点「始终允许」即可。
  - 前提是 Chrome 里登录着 AI Studio。cookie 过期了，就在浏览器里重新登录一次。
- 不想让它读浏览器：加 `--no-browser-cookie`，或者设置 `AISH_NO_BROWSER_COOKIE=1`。
- cookie 和 token 不会出现在任何输出里（`-v` 调试输出也会打码）。

## 配合 herdr
在 herdr 的 pane 里运行 `aish gpu` 即可。pane 里显示的就是 pod 上的真实画面，herdr 合盖或 detach 时 aish 继续运行，断线也会自己接回。

## 本地转义键（行首有效）
`~.` 断开（重连等待中按则放弃重连）· `~~` 发送一个 `~` · `~?` 帮助

## 注意 / 已知局限
- **不用 tmux 时，断开 ≠ 退出**：网关在断开时不回收远端 shell，远端 shell 退出时也不会关闭连接。普通 shell 模式下请用 `exit` 退出；aish 能识别出 `exit`/`logout`/^D 并自动结束。tmux 模式下没有这个问题：tmux 客户端结束后，aish 会让它所在的 shell 也退出。
- tmux 模式用 `tmux -u new-session -A -D -s <名字>`：`-D` 会踢掉同一会话上的其他客户端，包括你在浏览器 WebShell 里 attach 的。终端类型会优先用 `xterm-256color`。
- token 约 1 小时过期：断线重连时会重新换令牌。已经建立的连接在 token 过期后还能不能继续用，**没有实测**（页面脚本里也没有任何刷新逻辑）。
- `--exec` 用 `eval` 包住命令，用随机结束标记判断命令何时结束；命令如果自己执行 `exit`，会得到退出码 255。
- pod 如果用了 hostNetwork，`hostname` 显示的是节点名。

## 开发测试（不连生产 pod）
`dev/mock_server.py` 在本机模拟 webshellUrl / opsws / opsresize，后端是本机 bash 的 pty（二进制帧故意拆开 UTF-8；`MOCK_KEEP_OPEN=1` 模拟真实网关「shell 退出后不关 WS」；tmux 用 `/tmp/aish-mock-tmux` 隔离）。
```bash
MOCK_KEEP_OPEN=1 dev/mock_server.py 18765 /tmp/mock.log &
printf 'mockcookie123456' > /tmp/mock_cookie
export AIS_BASE_URL=http://127.0.0.1:18765 AIS_TOKEN_FILE=/tmp/mock_cookie AISH_HOSTS_FILE=/tmp/hosts.json AISH_NO_BROWSER_COOKIE=1
./aish add mk 'https://x/y?containerName=main&experimentId=1&podName=p&projectId=2&runId=3' --tmux aishmock
python3 dev/tmux_test.py ./aish mk --drop          # tmux：接入/断线重连/窗口/detach/接回/接管/结束
python3 dev/pty_test.py full x -- ./aish 'https://x/y?containerName=main&experimentId=1&podName=p&projectId=2&runId=3'   # 普通 shell
```
