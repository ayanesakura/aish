"""aish doctor：只读检查本机工具、cookie、保存的机器、Herdr 远端机器和 pod 环境。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import time

from aish_herdr import via_target

# 在 pod 上逐个检查命令：sh 的 `command -v a b c` 只看第一个参数，所以用循环。
PROBE = ('for c in python3 bash tmux claude node; do command -v "$c" >/dev/null 2>&1 '
         '&& echo "$c=ok" || echo "$c=missing"; done')
REQUIRED_ON_POD = ("python3", "bash")


class Doctor:
    def __init__(self, api, which=shutil.which, run=subprocess.run, out=print):
        self.api, self.which, self.run, self.out = api, which, run, out
        self.failed = self.warned = 0
        self.cache: dict = {}
        self.verified = False       # cookie 至少被平台接受过一次

    def ok(self, message):
        self.out("✓ " + message)

    def info(self, message):
        self.out("· " + message)

    def warn(self, message):
        self.warned += 1
        self.out("! " + message)

    def fail(self, message):
        self.failed += 1
        self.out("✗ " + message)

    def tools(self):
        if self.which("uv"):
            self.ok("本机有 uv")
        else:
            self.fail("本机缺少 uv，aish 无法运行")
        missing = [name for name in ("hc", "herdr-app") if not self.which(name)]
        if missing:
            self.warn("本机缺少 " + "、".join(missing) + "，无法使用 Herdr 接入（终端里直接用 aish 不受影响）")
        else:
            self.ok("本机有 hc 和 herdr-app")

    def runtime(self, home=None):
        current = (home or Path.home()) / ".local/share/aish/current"
        here = Path(__file__).resolve().parent
        if not current.exists():
            self.warn("没有安装共享 runtime，herdr-pod 命令和 skill 不可用；在仓库里运行 python3 install.py")
        elif here == current.resolve():
            self.ok(f"正在运行已安装的 runtime {here.name}")
        else:
            self.info(f"正在运行开发目录 {here}；已安装的 runtime 是 {current.resolve().name}")

    def cookie(self):
        path = self.api.token_file_path()
        if os.path.exists(path):
            return True
        self.fail(f"没有 cookie 文件 {path}；在浏览器登录 AI Studio 后运行 aish login")
        return False

    def runs(self, target):
        """实验下最近的 run；平台拒绝或出错时返回 None（同一实验只报告一次）。"""
        key = (target.base, target.project, target.experiment)
        if key not in self.cache:
            jar = self.api.CookieJar(target.base, allow_browser=False)
            try:
                self.cache[key] = self.api.list_runs(target, jar)
                if not self.verified:
                    self.verified = True
                    self.ok("cookie 有效（平台接口正常返回）")
            except self.api.AishError as error:
                self.cache[key] = None
                if getattr(error, "auth", False):
                    reason = "无权访问这个实验" if self.verified else "cookie 已失效或无权访问"
                    self.fail(f"平台拒绝了对实验 {target.experiment} 的请求（{reason}）；"
                              "在浏览器重新登录 AI Studio 后运行 aish login")
                else:
                    self.fail(f"查询实验 {target.experiment} 失败：{error}")
        return self.cache[key]

    @staticmethod
    def status_of(runs, run_id):
        return next((str(run.get("status")) for run in runs if str(run.get("id")) == str(run_id)), None)

    def hosts(self, only=None):
        hosts = self.api.load_hosts()
        if only and only not in hosts:
            raise self.api.AishError(f"未知的机器名 {only!r}（aish ls 查看）")
        names = [only] if only else sorted(hosts)
        if not names:
            self.info("还没有保存的机器（aish add <名字> '<web-shell URL>'）")
        for name in names:
            try:
                target, _ = self.api.host_target(name, {})
            except self.api.AishError as error:
                self.fail(f"{name}：{error}")
                continue
            runs = self.runs(target)
            if runs is None:
                continue
            if target.follow:
                latest = next((run for run in runs if str(run.get("status")) == "Running"), None)
                if latest:
                    self.ok(f"{name}：跟随实验 {target.experiment}，最新的运行中 run 是 {latest.get('id')}"
                            f"（{latest.get('name')}）")
                else:
                    self.warn(f"{name}：实验 {target.experiment} 下没有运行中的 run，现在连不上")
            else:
                status = self.status_of(runs, target.run)
                if status == "Running":
                    self.ok(f"{name}：固定的 run {target.run} 正在运行")
                else:
                    self.fail(f"{name}：固定的 run {target.run} 状态为 {status or '不在最近的 run 列表中'}，连不上")

    def machines(self):
        if not self.which("hc"):
            return
        try:
            listed = self.run(["hc", "machine", "ls"], capture_output=True, text=True, timeout=20)
            machines = json.loads(listed.stdout)["machines"]
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            self.fail("hc machine ls 没有返回有效结果；Herdr Core 可能没有运行")
            return
        ours = 0
        for machine in machines:
            try:
                words = shlex.split(machine.get("target") or "")
            except ValueError:
                continue
            script = next((word for word in words if Path(word).name in ("aish", "aish.py")), None)
            if not machine.get("via") or not script:
                continue        # ssh 机器或其他传输方式，不归 aish 管
            ours += 1
            self.machine(machine, script)
        if not ours:
            self.info("Herdr 里没有通过 aish 接入的远端机器")

    def machine(self, machine, script):
        name = machine["name"]
        if not os.path.exists(script):
            self.fail(f"Herdr 机器 {name}：传输命令里的 {script} 已不存在，断线后无法重连；"
                      "用 herdr-pod 重新接入同一个 pod")
            return
        target = via_target(machine, self.api)
        if target is None:
            self.warn(f"Herdr 机器 {name}：无法从传输命令解析出固定的 run/pod（可能引用了已删除的别名）")
            return
        runs = self.runs(target)
        if runs is None:
            return
        status = self.status_of(runs, target.run)
        if status is None:
            self.warn(f"Herdr 机器 {name}：run {target.run} 不在实验最近的 run 列表中，可能已结束")
        elif status != "Running":
            self.fail(f"Herdr 机器 {name}：指向的 run {target.run} 状态为 {status}，pod 已不可用。"
                      f"接入新 pod 用 aish herdr <别名> --latest；确认不再需要后 hc machine rm {name}")
            return
        state = machine.get("state")
        if state != "connected":
            self.fail(f"Herdr 机器 {name}：状态为 {state}（run {target.run} 仍在运行，cookie 有效）；"
                      "稍等自动重连，或在 Herdr 里重新连接")
            return
        started = time.monotonic()
        try:
            probe = self.run(["hc", "exec", "--on", name, PROBE], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.fail(f"Herdr 机器 {name}：显示已连接，但 30 秒内没有执行完检查命令")
            return
        elapsed = (time.monotonic() - started) * 1000
        if probe.returncode:
            self.fail(f"Herdr 机器 {name}：检查命令失败（退出码 {probe.returncode}）")
            return
        found = dict(line.split("=", 1) for line in probe.stdout.split() if "=" in line)
        missing = [command for command, state in found.items() if state != "ok"]
        self.ok(f"Herdr 机器 {name}：已连接 pod {target.pod}，往返 {elapsed:.0f}ms")
        blocking = [command for command in missing if command in REQUIRED_ON_POD]
        if blocking:
            self.fail(f"Herdr 机器 {name}：pod 上缺少 {'、'.join(blocking)}，aish 的管道传输依赖它")
        optional = [command for command in missing if command not in REQUIRED_ON_POD]
        if optional:
            self.warn(f"Herdr 机器 {name}：pod 的 PATH 里没有 {'、'.join(optional)}"
                      "（claude：无法新建 Claude Code；node：Claude 的 hooks 可能失败；tmux：不能用 aish 的常驻会话）")


def doctor(argv, api):
    parser = argparse.ArgumentParser(prog="aish doctor",
                                     description="检查 cookie、保存的机器、Herdr 远端机器和 pod 环境（只读）")
    parser.add_argument("name", nargs="?", help="只检查这台保存的机器（默认全部）")
    args = parser.parse_args(argv)
    check = Doctor(api)
    check.tools()
    check.runtime()
    if check.cookie():
        check.hosts(args.name)
        check.machines()
        if not check.cache:
            check.info("cookie 文件存在，但没有保存的机器可以用来验证它是否有效")
    print(f"\n{check.failed} 个问题，{check.warned} 个提醒" if check.failed or check.warned else "\n一切正常")
    return 1 if check.failed else 0
