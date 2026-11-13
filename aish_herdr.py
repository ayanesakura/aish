"""Register pinned AI Studio pods as native Herdr machines and spaces."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import urllib.parse

import tomlkit


def identity(target):
    return tuple(getattr(target, key) for key in
                 ("base", "project", "experiment", "run", "pod", "container"))


def canonical_url(target):
    parsed = urllib.parse.urlsplit(target.base)
    if parsed.username or parsed.password or parsed.scheme != "https":
        raise ValueError("Herdr 接入需要不含账号密码的 HTTPS WebShell URL。")
    query = urllib.parse.urlencode(dict(zip(
        ("projectId", "experimentId", "runId", "podName", "containerName"),
        identity(target)[1:])))
    return target.base + "/projects/modeling/experiment/web-shell?" + query


def machine_name(target):
    digest = hashlib.sha256(json.dumps(identity(target)).encode()).hexdigest()[:10]
    run = re.sub(r"[^a-z0-9-]", "-", target.run.lower())[:12]
    return f"pod-{run}-{digest}"


def via_target(machine, api):
    if not machine.get("via"):
        return None
    try:
        words = shlex.split(machine["target"])
        script = next(index for index, word in enumerate(words)
                      if Path(word).name in ("aish", "aish.py"))
        args = words[script + 1:]
        if not args or "--pipe" not in args:
            return None
        overrides = {}
        for field in ("run", "pod", "project", "experiment", "container"):
            if "--" + field in args:
                overrides[field] = args[args.index("--" + field) + 1]
        target, _ = api.host_target(args[0], overrides)
        return None if target.follow else target
    except (ValueError, KeyError, IndexError, StopIteration, api.AishError):
        return None


def shell_config(text):
    document = tomlkit.parse(text)
    if "shell" in document:
        return text
    document.add("shell", ["/bin/bash", "--norc"])
    return tomlkit.dumps(document)


class Connector:
    def __init__(self, api, tools):
        self.api = api
        self.tools = tools

    def run(self, tool, *args, source=None, timeout=180):
        try:
            result = subprocess.run([self.tools[tool], *args], input=source,
                                    text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise self.api.AishError(
                f"{tool} 超时；可能已完成部分接入，可用同一命令重试并复用已有资源。")
        if result.returncode:
            detail = self.api.REDACT((result.stderr or result.stdout)[-3000:])
            raise self.api.AishError(f"{tool} 执行失败：{detail}")
        return result.stdout

    def data(self, tool, *args, **kwargs):
        try:
            result = json.loads(self.run(tool, *args, **kwargs))
        except ValueError:
            raise self.api.AishError(f"{tool} 未返回有效 JSON；请检查该工具的版本和状态。")
        if isinstance(result, dict) and result.get("error"):
            raise self.api.AishError(self.api.REDACT(result["error"]))
        return result

    def remote(self, machine, action, payload):
        source = (Path(__file__).resolve().parent / "remote/herdr_setup.py").read_text()
        return self.data("hc", "exec", "--on", machine, "-i", "python3", "-",
                         action, json.dumps(payload), source=source)

    def attach(self, target, name=None, folder=None):
        url = canonical_url(target)
        machines = self.data("hc", "machine", "ls")["machines"]
        matching = [machine for machine in machines
                    if (other := via_target(machine, self.api))
                    and identity(other) == identity(target)]
        desired = name or machine_name(target)
        if name and not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
            raise self.api.AishError("--name 需为 1–32 位小写字母、数字或连字符，以字母开头。")
        collision = next((machine for machine in machines if machine["name"] == desired), None)
        if collision and collision not in matching:
            raise self.api.AishError(f"机器名 {desired} 已被其他连接使用，请换一个 --name。")
        if matching:
            machine = (collision or matching[0])["name"]
            self.api.note(f"复用远端机器 {machine}")
        else:
            machine = desired
            runtime = Path(__file__).resolve().with_name("aish.py")
            via = shlex.join([self.tools["uv"], "run", "--quiet", "--script", str(runtime), url, "--pipe"])
            self.api.note(f"注册远端机器 {machine}，安装 Herdr Core")
            self.run("hc", "machine", "add", "--name", machine, "--via", via,
                     "--install", timeout=300)
        self.api.note("检查 pod 环境和远端终端配置")
        probe = self.remote(machine, "probe", {"folder": folder})
        try:
            config = shell_config(probe["config"])
        except Exception as error:
            raise self.api.AishError(f"远端 Herdr 配置无法解析，保留原文件：{error}")
        prepared = self.remote(machine, "apply", {
            "config_before": probe["config"], "config_after": config,
            "node_source": probe["node_source"],
        })
        warnings = probe["warnings"] + prepared["warnings"]
        spaces = self.data("herdr-app", "ls")["spaces"]
        existing = next((space for space in spaces if space.get("machine") == machine
                         and space.get("folder") == probe["folder"]), None)
        created = existing is None
        if created:
            if probe["tmux_main"]:
                argv, tab = [probe["tmux"], "-u", "attach-session", "-t", "=main"], "main（已有会话）"
            elif probe["claude"]:
                argv, tab = [probe["claude"]], "Claude Code"
            else:
                argv, tab = ["/bin/bash", "--norc"], "Terminal"
            self.api.note(f"创建绑定 {machine} 的独立 space")
            self.data("herdr-app", "do", "space.new", json.dumps({
                "name": f"Pod {target.run} · {target.role or target.container}",
                "machine": machine, "folder": probe["folder"], "argv": argv, "tab": tab,
            }))
            spaces = self.data("herdr-app", "ls")["spaces"]
            existing = next((space for space in spaces if space.get("machine") == machine
                             and space.get("folder") == probe["folder"]), None)
        if not existing:
            raise self.api.AishError("机器已注册，但未找到绑定它的远端 space；请重试。")
        terminals = [terminal["terminal"] for tab in existing.get("tabs", [])
                     for terminal in tab.get("terminals", [])]
        if not terminals or not all(item.startswith(machine + "/") for item in terminals):
            raise self.api.AishError("space 的远端终端绑定验证失败；未启动额外会话。")
        self.data("herdr-app", "do", "space.select", json.dumps({"id": existing["id"]}))
        return {"machine": machine, "space": existing["id"], "name": existing["name"],
                "folder": probe["folder"], "created": created, "terminals": terminals,
                "warnings": warnings, "config_backup": prepared.get("config_backup")}


def connect(argv, api):
    parser = argparse.ArgumentParser(prog="aish herdr", description="一句命令接入原生 Herdr 远端 space")
    parser.add_argument("target", help="WebShell URL，或固定目标的 aish 别名")
    parser.add_argument("--name", help="新机器的名称（默认根据 pod 身份生成）")
    parser.add_argument("--folder", help="pod 上的工作目录（默认 verl、workspace 或 HOME）")
    args = parser.parse_args(argv)
    target, _ = api.host_target(args.target, {})
    if target.follow:
        raise api.AishError("请提供新 pod 的 WebShell URL，或用 aish add --pin 保存的固定别名。")
    try:
        canonical_url(target)
    except ValueError as error:
        raise api.AishError(str(error))
    tools = {name: shutil.which(name) for name in ("uv", "hc", "herdr-app")}
    missing = [name for name, path in tools.items() if not path]
    if missing:
        raise api.AishError("本机缺少命令：" + ", ".join(missing))
    api.CookieJar(target.base, allow_browser=True).get()
    lock_path = Path.home() / ".config/ais-webshell/herdr-connect.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise api.AishError("另一个 Herdr 接入任务正在执行，请等待其完成后重试。")
        result = Connector(api, tools).attach(target, args.name, args.folder)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
