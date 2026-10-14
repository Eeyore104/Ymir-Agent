"""Docker 沙箱快速验证（T03）：起会话 -> 限额/exec/超时/内存/文件/断网/销毁。

用途：验证本机 Docker 后端可用性与资源限额真实生效（换机 / 排障场景，
也是使用手册（T05）的预置自检脚本）。

用法：
  uv run python scripts/verify_docker_sandbox.py [--image python:3.12-slim]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import docker

from ymir.common.logging import setup_logging
from ymir.sandbox.base import NetworkPolicy
from ymir.sandbox.docker_sandbox import DockerSandbox


async def verify(image: str) -> bool:
    sandbox = DockerSandbox(image=image)

    print("[1/8] 探测 Docker daemon ...")
    if not sandbox.probe():
        print("      Docker 不可用（daemon 未启动？）")
        return False
    print("      OK")

    print(f"[2/8] 启动会话（镜像 {image}；首次需拉取，可能较慢）...")
    session = await sandbox.start(network=NetworkPolicy.NONE)
    sid = session.sandbox_id or "?"
    ok = True
    try:
        print(f"      session={sid[:12]}  network={session.network_policy}")

        # 3) 资源限额（直接读 daemon 侧容器配置）
        host = docker.from_env().containers.get(sid).attrs["HostConfig"]
        checks = [
            ("cpu_quota", host.get("CpuQuota"), 200_000),
            ("mem_limit", host.get("Memory"), 4 * 1024**3),
            ("pids_limit", host.get("PidsLimit"), 512),
            ("network_mode", host.get("NetworkMode"), "none"),
        ]
        print("[3/8] 资源限额（daemon 实际配置）:")
        for name, actual, expected in checks:
            hit = actual == expected
            ok &= hit
            print(f"      {name}: {actual}  (期望 {expected})  {'✓' if hit else '✗'}")

        # 4) exec
        result = await session.exec('python -c "print(6*7)"')
        hit = result.exit_code == 0 and "42" in result.stdout
        ok &= hit
        print(
            f"[4/8] exec: exit={result.exit_code} stdout={result.stdout.strip()!r}"
            f" ({result.duration_ms}ms)  {'✓' if hit else '✗'}"
        )

        # 5) 超时
        result = await session.exec("sleep 30", timeout=2)
        hit = result.timed_out
        ok &= hit
        print(
            f"[5/8] 超时保护: timed_out={result.timed_out}"
            f" exit={result.exit_code}  {'✓' if hit else '✗'}"
        )

        # 6) 内存硬限额（申请 6GB > 4g 上限 → cgroup OOM kill，exit 137）
        #    注意：bytearray 是延迟分配（overcommit），必须 touch 每个物理页才会真实占内存；
        #    沙箱已设 memswap_limit=memory（硬上限，无额外 swap 池）。
        result = await session.exec(
            "python -c \"d = bytearray(6*1024**3); d[::4096] = b'\\x01' * 1572864\"",
            timeout=120,
        )
        hit = result.exit_code in (137, -9)
        ok &= hit
        print(
            f"[6/8] 内存限额: exit={result.exit_code}"
            f"（期望 137 = OOM 被杀）  {'✓' if hit else '✗'}"
        )

        # 7) 文件读写
        await session.write_file("hello.txt", "hello docker sandbox")
        content = await session.read_file("hello.txt")
        listing = await session.list_files(".")
        hit = content == "hello docker sandbox" and "hello.txt" in listing
        ok &= hit
        print(f"[7/8] 文件读写: read={content!r} list={listing}  {'✓' if hit else '✗'}")

        # 8) 断网验证（network=none 下外连必须失败）
        cmd = (
            "python -c \"import socket; socket.setdefaulttimeout(3); "
            "socket.create_connection(('1.1.1.1', 443))\""
        )
        result = await session.exec(cmd, timeout=15)
        hit = result.exit_code != 0
        ok &= hit
        print(f"[8/8] 断网验证: exit={result.exit_code}（期望非 0）  {'✓' if hit else '✗'}")
    finally:
        await sandbox.destroy(session)
        print("      会话已销毁 ✓")

    print("=" * 60)
    print("结果:", "PASS ✓ 沙箱后端与资源限额全部生效" if ok else "FAIL ✗ 存在未通过项")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Docker 沙箱快速验证（T03）")
    parser.add_argument("--image", default="python:3.12-slim", help="沙箱镜像")
    args = parser.parse_args()
    setup_logging()
    return 0 if asyncio.run(verify(args.image)) else 1


if __name__ == "__main__":
    sys.exit(main())
