"""验证 aish 设置的 tmux 默认选项（在 pane 里只运行只读的 tmux show / echo）。
用法: python3 tmux_opts_test.py <期望提示符片段或 -> -- <aish 及参数>"""
import os, re, sys
here = os.path.dirname(os.path.abspath(__file__))
ns = {"__file__": os.path.join(here, "tmux_test.py")}
exec(open(os.path.join(here, "tmux_test.py")).read().split("def start_mock")[0], ns)
Pty, step = ns["Pty"], ns["step"]
want_prompt = sys.argv[1]
p = Pty(sys.argv[sys.argv.index("--") + 1:], rows=30, cols=120)
p.expect(rb"\x1b\[\?1049h", 30); p.expect(rb"[$%] ", 20); p.quiet(1.5)
scr = Pty.show(p.buf)
last = [l for l in scr.splitlines() if l.strip().endswith(("$", "%"))]
step(f"pane 提示符: {last[-1].strip() if last else '?'}"
     + ("" if want_prompt == "-" else f"   含「{want_prompt}」= {want_prompt in scr}"))
mark = len(p.buf)
p.send('echo "OPT mouse=$(tmux show -gv mouse) hist=$(tmux show -gv history-limit) esc=$(tmux show -sv escape-time) '
       'dterm=$(tmux show -gv default-terminal) dcmd=$(tmux show -gv default-command) rgb=$(tmux show -s terminal-features | grep -c RGB) '
       'TERM=$TERM COLORTERM=${COLORTERM:-空}"\r')
m = p.expect(rb"\nOPT ([^\r\n]+)", start=mark); p.quiet(0.8)
step("选项: " + m.group(1).decode())
if os.environ.get("PROBE"):
    mark = len(p.buf)
    p.send('echo "PROBE ' + os.environ["PROBE"] + '"\r')
    m = p.expect(rb"\nPROBE ([^\r\n]+)", start=mark); p.quiet(0.8)
    step("探测: " + m.group(1).decode())
mark = len(p.buf)
p.send(b"\x1b[<64;30;10M")          # SGR 鼠标：滚轮向上
p.quiet(1.0)
cm = re.search(rb"\[\d+/\d+\]", p.buf[mark:])
step(f"滚轮向上 → 进入复制模式（右上角出现 [行/总行]）= {bool(cm)}")
p.send("q"); p.quiet(0.8)
p.send("exit\r")
rc, restored, msgs = p.finish()
step(f"exit → 退出码={rc} 终端已恢复={restored} 提示={msgs[-1] if msgs else None}")
