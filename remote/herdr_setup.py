"""Small remote environment probe and additive setup, executed through hc exec."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def config_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "herdr-core/config.toml"


def read_config(path):
    return path.read_text() if path.exists() else ""


def probe(payload):
    home = Path.home()
    folder = payload.get("folder")
    if folder:
        directory = Path(folder).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise RuntimeError("--folder 必须是 pod 上已存在的绝对目录")
    else:
        directory = next(path for path in (Path("/workspace/verl"), Path("/workspace"), home) if path.is_dir())
    if not Path("/bin/bash").exists():
        raise RuntimeError("pod 缺少 /bin/bash，无法配置 Bash 终端")
    warnings = []
    claude = shutil.which("claude")
    if not claude:
        warnings.append("pod 的 PATH 中没有 claude；已接入终端，但需要在 pod 安装/配置 Claude 后才能使用 + Claude Code。")
    node_source = None
    if not shutil.which("node"):
        result = subprocess.run(["/bin/bash", "-c",
                                 'export NVM_DIR="$HOME/.nvm"; '
                                 '[ -s "$NVM_DIR/nvm.sh" ] && '
                                 '. "$NVM_DIR/nvm.sh" --no-use && nvm which default'],
                                capture_output=True, text=True, timeout=20)
        candidate = Path(result.stdout.strip()) if result.returncode == 0 else None
        if candidate and candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            node_source = str(candidate)
        else:
            warnings.append("pod 中未找到 Node，Claude 的 Node hooks 可能失败；未自动下载安装。")
    tmux = shutil.which("tmux")
    tmux_main = bool(tmux and subprocess.run([tmux, "has-session", "-t", "=main"],
                                            capture_output=True, timeout=5).returncode == 0)
    return {"folder": str(directory), "config": read_config(config_path()),
            "claude": claude, "node_source": node_source, "tmux": tmux,
            "tmux_main": tmux_main, "warnings": warnings}


def apply(payload):
    path = config_path()
    before, after = payload["config_before"], payload["config_after"]
    if read_config(path) != before:
        raise RuntimeError("Herdr 配置已被其他进程修改，请重试；没有覆盖文件")
    backup = None
    warnings = []
    if before != after:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            descriptor, backup = tempfile.mkstemp(prefix="config.toml.backup-", dir=path.parent)
            os.close(descriptor)
            shutil.copy2(path, backup)
        descriptor, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(after)
            if path.exists():
                shutil.copymode(path, temporary)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if payload.get("node_source"):
        source = Path(payload["node_source"])
        destination = Path.home() / ".local/bin/node"
        if str(destination.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            warnings.append("~/.local/bin 不在 Herdr Core 的 PATH 中；未创建无效的 Node 链接。")
        elif not os.path.lexists(destination) and source.is_file() and os.access(source, os.X_OK):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(source)
        elif not shutil.which("node"):
            warnings.append("~/.local/bin/node 已存在或 Node 来源不可用；保留原路径，请检查 Node。")
    return {"config_backup": backup, "warnings": warnings}


if __name__ == "__main__":
    try:
        result = {"probe": probe, "apply": apply}[sys.argv[1]](json.loads(sys.argv[2]))
        print(json.dumps(result))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
