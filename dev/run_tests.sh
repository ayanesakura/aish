#!/bin/sh
# 不连真实 pod 的自动化测试（CI 也跑这个）。需要 uv；pipe_test 会在本机起一个 mock WebShell。
set -e
cd "$(dirname "$0")/.."
export AISH_NO_NOTIFY=1 AISH_NO_BROWSER_COOKIE=1
for name in herdr_test doctor_test pipe_test; do
    echo "== dev/$name.py"
    uv run --quiet --with 'websockets>=14' --with 'cryptography>=42' --with 'tomlkit>=0.13' python "dev/$name.py"
done
