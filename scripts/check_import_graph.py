"""依赖方向 / 循环依赖静态护栏（ARCH-15）。

零第三方依赖：用 ``ast`` 解析 ``src/ymir/**/*.py``，构建「子包 -> 子包」依赖
图，校验两件事：

1. **分层方向**：定义 ymir 专有的分层契约（序号越大越内层，外层可依赖内层），
   任何「内层依赖外层」的逆层边判为违规；
2. **循环依赖**：子包级 DFS 检出环。

作为 CI 门禁运行（``uv run python scripts/check_import_graph.py``），并在
``tests/test_import_graph.py`` 中以定点断言复用同一实现。

分层契约（按修复后的实际依赖图校准，docs/02 §2.2/§5）：

    api(0) > bootstrap/orchestrator/evaluation(1)
           > hitl/roles/sop/messages/events(2)
           > tools/sandbox/gateway/artifacts(3)
           > storage(4)
           > models/common/config(5)

合规边示例：``orchestrator -> hitl``（1->2，外层依赖内层，合法）。
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "ymir"

#: 分层契约：序号越大越内层；边 src->dst 合法当且仅当 rank(src) <= rank(dst)。
LAYERS: dict[str, int] = {
    "api": 0,
    "bootstrap": 1,
    "orchestrator": 1,
    "evaluation": 1,
    "hitl": 2,
    "roles": 2,
    "sop": 2,
    "messages": 2,
    "events": 2,
    "tools": 3,
    "sandbox": 3,
    "gateway": 3,
    "artifacts": 3,
    "storage": 4,
    "models": 5,
    "common": 5,
    "config": 5,
    # 顶层包/模块（通常无跨包边）：占位以免误报
    "ymir": 0,
    "__init__": 0,
}


@dataclass(frozen=True)
class Violation:
    """一条分层违规边（内层依赖了更外层）。"""

    src: str
    dst: str
    src_rank: int
    dst_rank: int
    evidence: tuple[str, ...]

    def __str__(self) -> str:
        return (
            f"{self.src}(rank {self.src_rank}) -> {self.dst}(rank {self.dst_rank}) "
            f"{list(self.evidence)}"
        )


def _to_subpkg(rel_path: str) -> str:
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) == 1:
        return parts[0][:-3] if parts[0].endswith(".py") else parts[0]
    return parts[0]


def _module_name(rel_path: str) -> str:
    p = rel_path.replace("\\", "/")
    if p.endswith("__init__.py"):
        p = p[: -len("__init__.py")].rstrip("/")
    elif p.endswith(".py"):
        p = p[:-3]
    return p.replace("/", ".")


def _parse_imports(content: str) -> list[tuple[str, int]]:
    """返回 ``[(import 目标模块名, 行号)]``（含相对导入的 ``.`` 前缀）。"""
    out: list[tuple[str, int]] = []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                out.append(("." * node.level + (node.module or ""), node.lineno))
            elif node.module:
                out.append((node.module, node.lineno))
    return out


def _iter_py_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        files[rel] = path.read_text(encoding="utf-8")
    return files


def _collect_edges(root: Path) -> dict[tuple[str, str], set[str]]:
    """构建 ``(src_pkg, dst_pkg) -> {证据}`` 的跨子包依赖边。

    相对导入按「当前模块所属包」解析为绝对模块名（补回 ``ymir.`` 前缀），
    否则 ``from ..roles import x`` 会被误解析为 ``roles`` 并因缺前缀而丢弃，
    导致相对导入的跨包边不可见（QA-15-1）。
    """
    edges: dict[tuple[str, str], set[str]] = defaultdict(set)
    for rel, content in _iter_py_files(root).items():
        src_pkg = _to_subpkg(rel)
        rel_mod = _module_name(rel)
        src_mod = f"ymir.{rel_mod}" if rel_mod else "ymir"
        for target, lineno in _parse_imports(content):
            if target.startswith("."):
                depth = len(target) - len(target.lstrip("."))
                base = src_mod.split(".")
                cur = base if rel.endswith("__init__.py") else base[:-1]
                cur = cur[: len(cur) - (depth - 1)]
                tail = target.lstrip(".")
                resolved = ".".join(cur + ([tail] if tail else []))
            elif target == "ymir" or target.startswith("ymir."):
                resolved = target
            else:
                continue
            if not (resolved == "ymir" or resolved.startswith("ymir.")):
                continue
            target_rel = resolved[len("ymir.") :] if resolved != "ymir" else ""
            dst_pkg = target_rel.split(".")[0] if target_rel else "ymir"
            if src_pkg == dst_pkg:
                continue
            edges[(src_pkg, dst_pkg)].add(f"{rel}:{lineno} -> {resolved}")
    return edges


def _find_cycles(edges: dict[tuple[str, str], set[str]]) -> list[list[str]]:
    adj: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for src, dst in edges:
        adj[src].add(dst)
        nodes.add(src)
        nodes.add(dst)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(nodes, WHITE)
    cycles: list[list[str]] = []
    stack: list[str] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in sorted(adj.get(u, ())):
            if color.get(v, WHITE) == WHITE:
                dfs(v)
            elif color.get(v) == GRAY:
                i = stack.index(v)
                cycles.append(stack[i:] + [v])
        stack.pop()
        color[u] = BLACK

    for n in sorted(nodes):
        if color.get(n, WHITE) == WHITE:
            dfs(n)
    return cycles


def analyze(root: Path = SRC_ROOT) -> tuple[list[Violation], list[list[str]]]:
    """返回 ``(分层违规, 循环依赖)``；两者皆空表示依赖契约成立。"""
    edges = _collect_edges(root)
    violations: list[Violation] = []
    for (src, dst), evidence in sorted(edges.items()):
        src_rank = LAYERS.get(src)
        dst_rank = LAYERS.get(dst)
        if src_rank is None or dst_rank is None:
            # 未分层的子包同样视为违规：新增包须显式纳入分层契约
            violations.append(
                Violation(src, dst, src_rank if src_rank is not None else -1,
                          dst_rank if dst_rank is not None else -1, tuple(sorted(evidence)))
            )
            continue
        if src_rank > dst_rank:
            violations.append(Violation(src, dst, src_rank, dst_rank, tuple(sorted(evidence))))
    return violations, _find_cycles(edges)


def main() -> int:
    violations, cycles = analyze()
    print("=" * 70)
    print("依赖护栏：分层方向 + 循环依赖（src/ymir）")
    print("=" * 70)
    if violations:
        print(f"[FAIL] 分层违规 {len(violations)} 条：")
        for item in violations:
            print(f"  - {item}")
    else:
        print("[OK] 无分层违规")
    if cycles:
        print(f"[FAIL] 子包级循环依赖 {len(cycles)} 条：")
        for cycle in cycles:
            print("  - " + " -> ".join(cycle))
    else:
        print("[OK] 无子包级循环依赖")
    ok = not violations and not cycles
    print("=" * 70)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
