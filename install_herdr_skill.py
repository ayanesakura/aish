"""Install a versioned runtime and one shared skill for Codex and Claude Code."""

import hashlib
import json
from pathlib import Path
import shlex
import shutil
import tempfile


SOURCE = Path(__file__).resolve().parent
FILES = ("aish.py", "aish_pipe.py", "aish_remote.py", "aish_herdr.py",
         "remote/herdr_setup.py", "skills/herdr-pod/SKILL.md",
         "skills/herdr-pod/agents/openai.yaml")


def managed_link(path, target, allowed_root):
    if path.is_symlink():
        if not path.resolve().is_relative_to(allowed_root.resolve()):
            raise RuntimeError(f"保留已有非本工具链接：{path}")
        return
    if path.exists():
        raise RuntimeError(f"保留已有文件/目录：{path}；请先确认它的用途")


def replace_link(path, target):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".herdr-install-", dir=path.parent) as temporary:
        link = Path(temporary) / "link"
        link.symlink_to(target)
        link.replace(path)


def install(home=None):
    home = home or Path.home()
    root = home / ".local/share/aish"
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("本机缺少 uv；请安装后重试")
    contents = {name: (SOURCE / name).read_bytes() for name in FILES}
    launcher = '#!/bin/sh\nexec ' + shlex.quote(uv) + ' run --quiet --script ' + shlex.quote(
        str(root / "current/aish.py")) + ' herdr "$@"\n'
    contents["herdr-pod"] = launcher.encode()
    digest = hashlib.sha256()
    for name, content in sorted(contents.items()):
        digest.update(name.encode() + b"\0" + content + b"\0")
    release = root / "releases" / digest.hexdigest()[:16]
    skill = root / "current/skills/herdr-pod"
    links = {
        root / "current": release,
        home / ".agents/skills/herdr-pod": skill,
        home / ".claude/skills/herdr-pod": skill,
        home / ".local/bin/herdr-pod": root / "current/herdr-pod",
    }
    for path, target in links.items():
        managed_link(path, target, root)
    if release.exists():
        if any(not (release / name).is_file() or (release / name).read_bytes() != content
               for name, content in contents.items()):
            raise RuntimeError(f"已安装版本内容有变化，保留现场：{release}")
    else:
        release.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=release.parent) as temporary:
            staging = Path(temporary) / "runtime"
            staging.mkdir()
            for name, content in contents.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            (staging / "herdr-pod").chmod(0o755)
            staging.rename(release)
    previous = str((root / "current").resolve()) if (root / "current").exists() else None
    for path, target in links.items():
        replace_link(path, target)
    return {"runtime": str(release), "previous_runtime": previous,
            "command": str(home / ".local/bin/herdr-pod"),
            "codex_skill": str(home / ".agents/skills/herdr-pod"),
            "claude_skill": str(home / ".claude/skills/herdr-pod")}


if __name__ == "__main__":
    try:
        print(json.dumps(install(), ensure_ascii=False, indent=2))
    except (OSError, RuntimeError) as error:
        raise SystemExit(str(error))
