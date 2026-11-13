import os, subprocess, sys, time, json
here = os.path.dirname(os.path.abspath(__file__))
ns = {"__file__": os.path.join(here, "tmux_test.py")}
exec(open(os.path.join(here, "tmux_test.py")).read().split("def start_mock")[0].replace("AISH, HOST = sys.argv[1], sys.argv[2]", "AISH = HOST = None"), ns)
Pty, step = ns["Pty"], ns["step"]
def start_mock():
    subprocess.Popen([os.path.join(here, "mock_server.py"), "18765", os.path.join(here, "mock.log")],
                     env={**os.environ, "MOCK_KEEP_OPEN": "1", "MOCK_RUNS_FILE": os.path.join(here, "mock_runs.json")},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(2.5)
p = Pty([sys.argv[1], "fl"])
p.expect(rb"\x1b\[\?1049h", 30); p.quiet(1.0); step("已接入 run 3001 的 tmux")
subprocess.run(["pkill", "-f", "mock_server.py"]); step("杀掉 mock（模拟断线），并把 run 换成 3002")
json.dump([{"id": 3002, "name": "run-newer", "status": "Running", "createTime": 3000,
            "pods": [{"podName": "run-newer-3002-master-0", "status": "Running", "mainContainer": "main", "showWebshell": True}]},
           {"id": 3001, "name": "run-new", "status": "Stopped", "createTime": 2000}], open(os.path.join(here, "mock_runs.json"), "w"))
p.expect("重连".encode(), 20); time.sleep(1); start_mock(); step("mock 已重启")
mark = len(p.buf)
p.expect("已重连".encode(), 40, start=mark); p.quiet(1.5)
msgs = [x.decode() for x in __import__("re").findall(rb"\[aish\] [^\r\n]*", p.buf)]
for m in msgs: print("     ", m)
p.send(b"\x02d")
rc, restored, msgs = p.finish()
step(f"detach → 退出码={rc} 终端已恢复={restored}")
