# aish — AI Studio WebShell 终端客户端

在 mac 终端里直接接入 AI Studio 训练 pod 的 shell，效果类似 `ssh -t` / `herdr --remote`。
mac 和 pod 之间没有直连网络，aish 走的是平台网页 WebShell 背后的那条 WebSocket。

- **会话常驻**：默认接入 pod 上的 tmux 会话。关终端、断网、合盖都不影响 pod 上正在跑的东西（比如 Claude Code）。
- **自动重连**：断线后自动重新换令牌，接回同一个 tmux 会话。会主动探测连接是否还活着（WebSocket ping），网络断开后约 25 秒内就能发现。
- **按名字连接**：`aish add` 保存一次，以后只需要 `aish gpu`。
- **自动跟随最新的 run**：训练重启或换了 run，pod 名会变，aish 每次连接都会自动找到实验下最新的运行中 run，连接同一角色的 pod（如 `master-0`）。
- **cookie 自动获取**：直接读取本机 Chrome 里的 `ais_token`，过期了也会自动刷新。

## 安装
需要 [uv](https://docs.astral.sh/uv/)。依赖（websockets、cryptography）在第一次运行时自动安装。
Python 入口是 `aish.py`，`aish` 只是调用它的两行 shell 启动器；`aish_pipe.py` 和 `aish_remote.py` 必须放在同一目录。也可始终使用 `uv run --script /绝对路径/aish.py`。
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
- 训练换了 run：下次 `aish gpu`（或断线重连时）会提示「实验 … 最新的运行中 run 是 …，连接 pod …」，然后自动连到新 pod。新 pod 上是一个新的 tmux 会话，旧会话随旧 pod 一起结束。
  - 实验下同时有多个 run 在运行时，连接最新创建的那个；要连其它的，用 `aish gpu --run <id>`。
  - 不想跟随：保存时加 `--pin`（`aish add gpu '<URL>' --pin`），固定连这个 run/pod。
- 在另一台电脑或另一个终端运行 `aish gpu`：会接管这个会话，旧的客户端自动退出。
- 在 tmux 里 `exit` 掉最后一个 shell：tmux 会话结束，aish 随之退出。

### aish 给 tmux 设置的默认选项
如果你在 pod 上没有自己的 `~/.tmux.conf`，每次 `aish gpu` 接入时 aish 会设置这些选项（有自己的配置就完全不动，以你的为准；设置 `AISH_TMUX_DEFAULTS=0` 也可以关掉）：
| 选项 | 效果 |
|---|---|
| `mouse on` | **滚轮直接翻看历史输出**（进入复制模式，按 `q` 退出），可以用鼠标点选 pane 和拖动分隔线；拖选文字会复制到 tmux，并经 OSC 52 写入 mac 剪贴板 |
| `history-limit 100000` | 每个窗口保留 10 万行历史 |
| `escape-time 10` | tmux 3.4 默认 500ms，会让你在 Claude 里按 Esc 中断时卡一下 |
| `default-terminal tmux-256color`；本地支持真彩色时再加 RGB 和 `COLORTERM=truecolor` | Claude 的配色完整显示 |
| 新窗口用 `bash --norc`，并恢复平台的 `PS1` | 和网页 WebShell 一致，带 pod 名的提示符 |

- 想用终端自带的选择（不经过 tmux）：Ghostty / iTerm2 里**按住 Shift 拖选**。
- 鼠标、Esc 这类选项接入时立即生效；历史行数和提示符只对**新开的窗口**（`Ctrl-b c`）生效，已有窗口保持原样。

其他用法：
```bash
aish gpu --exec 'nvidia-smi'       # 非交互：打印输出，按远端退出码退出（--timeout 默认 30s，超时退出码 124）
aish gpu --tmux work               # 临时用另一个 tmux 会话名
aish gpu --no-tmux                 # 普通 shell，不用 tmux（断线不会自动重连）
aish gpu -- htop                   # 普通 shell，连上后自动输入命令
aish ls / aish rm gpu              # 列出 / 删除保存的机器（~/.config/ais-webshell/hosts.json）
aish runs gpu                      # 查看实验下最近的 run（状态、启动时间）和运行中 run 的 pod
aish gpu --run 2459535             # 临时连接指定的 run（不跟随）
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
**要让 Herdr 的「+ → Claude Code」在 pod 上创建新会话，必须使用绑定远端 machine 的 space**，见下方「Herdr 原生远端机器」。仅在本机 space 的 pane 里运行 `aish gpu`，只能操作那个远端终端；该 space 的「+」仍会在 Mac 上新建程序。space 的名字不决定程序运行位置。

### 控制 pod 上已有的 Claude 会话

如果 Claude 已经在 tmux 的 `main` 会话中，在 Mac Herdr 的终端里运行：

```bash
uv run --script /绝对路径/aish.py gpu --run <runId> --pod <podName> --attach main --no-browser-cookie
```

`--attach` 只附着已有会话：不创建 tmux server 或会话、不修改 tmux 选项、不踢掉原客户端；断线后仍会自动附着同一会话。它要求固定 run/pod，避免训练重启后接到另一台机器。没有目标会话时会显示 tmux 错误，保留普通 shell，不创建新会话。

此时 Herdr 中的键盘、粘贴、Esc、Ctrl-C 都传给 pod 上原来的 Claude。多个客户端共享输入和终端尺寸。用 `Ctrl-b d` 或行首 `~.` 离开，不会结束 Claude。普通 WebShell 中、没有 tmux 保护的既有 Claude 进程不能自动迁入此模式。

这是 Herdr 中的远端终端画面。Claude 的结构化状态、聊天历史和会话列表不会因此自动成为 Herdr 原生远端会话。

### Herdr 原生远端机器（需要 pod 上的 hc）

`--pipe` 提供 `hc machine add --via` 要求的双向字节流：标准输入、标准输出、标准错误和退出码分别传输；不会把提示符、回显或连接信息混入 stdout。它通过明文 Python 源码启动管道代理，再用 base64 帧传递二进制数据，以适配平台只接受文本输入的 WebShell。pod 需要 `python3`。

先用 `aish login` 在本地准备有效 cookie，然后注册固定 run/pod：

```bash
hc machine add --name training --via 'uv run --script /绝对路径/aish.py gpu --run <runId> --pod <podName> --pipe'
```

pod 缺少 `hc` 时，上述命令不会静默安装。确认允许在 pod 安装后，可在 `hc machine add` 后追加 `--install`。随后创建远端 space：

```bash
herdr-app do space.new '{"name":"Training pod","machine":"training","folder":"/workspace/verl"}'
hc ls
```

在新 space 中启动的 Claude 由远端 Herdr Core 管理。安装 `hc` 不会接管已经运行在其他 WebShell 或 tmux 中的 Claude；继续使用旧会话请用前面的 `--attach`。

在绑定远端机器的 space 中点顶部「+ → Claude Code」即可在 pod 新建 Claude；「+ → Terminal」也会在 pod 打开 shell。用 `herdr-app ls` 检查该 space 的 `machine` 是注册的机器名，终端地址应带 `training/` 前缀。只看 space 名称或某个 pane 的远端画面不足以确认这一点。

已有 tmux 会话也可以放进同一个原生远端 space：通过 `tab.new` 的 `argv` 传入 `["tmux", "-u", "attach-session", "-t", "=main"]`。此时 tmux 客户端本身就在 pod，不再需要每个 pane 单独运行本地 aish。

远端 Claude 的启动 hook 可能依赖 Node。注册后可用 `hc exec --on training 'command -v claude; command -v node'` 检查 Herdr 实际使用的 PATH。若 Node 只装在 nvm 内，应将已经安装且选中的 Node 暴露给这个环境；不要用 Mac 上是否存在 Node 来判断。

如果「+ → Terminal」原样显示 `\[\033[32m\]`、`\w`，检查终端的实际 shell。平台导出的 `PS1` 使用 Bash 语法，而缺少 `SHELL` 的环境可能让 Herdr 启动 `/bin/sh`。在 **pod** 的 `~/.config/herdr-core/config.toml` 中设置 `shell = ["/bin/bash", "--norc"]`，新终端就会用 Bash 解释平台提示符。`remote/herdr-core.toml` 提供这一最小配置；已有配置时只合并 `shell` 项。当前空闲 shell 可运行 `exec /bin/bash --norc`，不需要重启 Claude 或 tmux。

`--pipe` 不读取浏览器/钥匙串，也不自动重放命令；cookie 过期时先刷新 cookie，再由 Herdr 重连。`--timeout` 在管道模式下只限制各启动握手阶段，不限制会话总时长。连接结束会通知代理退出；网络丢失时代理在约 45 秒无心跳后清理自己启动的命令进程组。独立运行的 tmux/Herdr Core 服务不属于该进程组。

本地验收：

```bash
python3 dev/pipe_test.py
```

测试覆盖全字节 1 MiB 往返、stderr/退出码、EOF、提前关闭 stdin、取消连接、拒绝自动跟随 run、两个客户端共享附着且保持 tmux 配置，以及真实 `hc machine add --via` / `hc exec -i` 接口。所有测试服务和 hc 配置均隔离；不连接生产 pod。没有安装 hc/tmux 时跳过对应集成测试。

## 本地转义键（行首有效）
`~.` 断开（重连等待中按则放弃重连）· `~~` 发送一个 `~` · `~?` 帮助

## 注意 / 已知局限
- **不用 tmux 时，断开 ≠ 退出**：网关在断开时不回收远端 shell，远端 shell 退出时也不会关闭连接。普通 shell 模式下请用 `exit` 退出；aish 能识别出 `exit`/`logout`/^D 并自动结束。tmux 模式下没有这个问题：tmux 客户端结束后，aish 会让它所在的 shell 也退出。
- OSC 52 剪贴板依赖本地终端支持（Ghostty、iTerm2 支持），没有逐一实测。
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
./aish add mk 'https://x/y?containerName=main&experimentId=1&podName=p&projectId=2&runId=3' --tmux aishmock --pin
python3 dev/tmux_test.py ./aish mk --drop          # tmux：接入/断线重连/窗口/detach/接回/接管/结束
python3 dev/tmux_opts_test.py 'mock$' -- ./aish mk  # tmux 默认选项、提示符、滚轮进入复制模式
# 在真实 pod 上测试时，用 AISH_TMUX_SOCKET=aish-test 走独立的 tmux socket，不影响正在用的会话
python3 dev/pty_test.py full x -- ./aish 'https://x/y?containerName=main&experimentId=1&podName=p&projectId=2&runId=3'   # 普通 shell
```
