"""T03.10：``docker/sandbox.Dockerfile`` ↔ 模板镜像包表的**一致性钉子**（v1.5）。

**要防的裂缝**：``requires_deps`` 的判定依据是"交付树依赖声明 ∩ 镜像预装清单"，而
清单有两份：``templates.models.SANDBOX_IMAGE_PACKAGES`` / ``..._NODE_PACKAGES``
（仓内表）与镜像里**真实装了**的包（Dockerfile / ``docker/node-baseline/package.json``）。
两份漂移时：

- **表比镜像大**（表里有、镜像没装）⇒ 模板"能加载、检查跑起来却 ImportError"⇒ 误判
  **代码坏了**（最坏方向）；
- **表比镜像小**（镜像装了、表没登记）⇒ 声明了却被判"缺失"⇒ 误 **skip**（方向宽松，
  但会静默降级成"未验证"）。

本条用**集合相等**把两侧钉死：任一方向漂移都 CI 必红。方向也安全 —— 表比镜像小是
唯一"误 skip"的方向，而它同样被相等断言覆盖。

**为什么不用镜像指纹（R1 候选②）**：那需要在运行期 ``cat /opt/image-manifest.json``
（多一次沙箱调用，与"零额外沙箱调用"纪律冲突），且镜像内清单仍是**另一份要人工同步
的表** —— 只是把漂移从「仓内表↔镜像」换成「镜像内文件↔仓内表」，没有消除漂移。
静态对齐 + CI 钉子把"运行期不可知"变成"CI 必红"，成本低且方向正确。

纪律：零 IO 副作用（只读仓库文件）、零 Docker。
"""

from __future__ import annotations

import json
import re
import shlex

from ymir.config import PROJECT_ROOT
from ymir.templates.loader import image_packages_for
from ymir.templates.models import SANDBOX_IMAGE_NODE_PACKAGES, SANDBOX_IMAGE_PACKAGES

DOCKERFILE = PROJECT_ROOT / "docker" / "sandbox.Dockerfile"
NODE_BASELINE = PROJECT_ROOT / "docker" / "node-baseline" / "package.json"

#: 带值的 pip 选项（下一个 token 是值，不是 requirement）。
_PIP_OPTIONS_WITH_VALUE: frozenset[str] = frozenset(
    {
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "-t",
        "--target",
        "--retries",
        "--timeout",
        "--trusted-host",
        "-c",
        "--constraint",
    }
)

#: PEP 508 名字右边界（``pytest>=8.2,<9`` → ``pytest``；``sqlalchemy[asyncio]`` → ``sqlalchemy``）。
_NAME_SPLIT = re.compile(r"[<>=!~\[;(\s]")


def _logical_lines(text: str) -> list[str]:
    """把 Dockerfile 的 ``\\`` 续行拼成**逻辑行**。

    ★ 必须按逻辑行处理，不能把整个文件拼成一行：那会让一条 ``RUN ... pip install`` 的
    尾部与**下一条指令**（``COPY`` / ``RUN ln ...``）混在同一段里，解析出一堆路径当包名
    （首版就是这么红的）。
    """
    lines: list[str] = []
    buffer = ""
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        lines.append(f"{buffer}{line}")
        buffer = ""
    if buffer:
        lines.append(buffer)
    return lines


def _pip_requirements(text: str) -> set[str]:
    """从 Dockerfile 正文里抽出**全部** ``pip install`` 的包名集合。

    实现要点（只够解析本仓库这一种写法，够用且不引依赖）：

    1. 按 ``\\`` 续行拼成逻辑行（一条 ``RUN`` = 一个逻辑行）；
    2. 只在含 ``pip install`` 的逻辑行里，按 ``&&`` 切成若干命令；
    3. ``shlex.split`` 后只认前两个 token 是 ``pip install`` 的段，再逐 token 判定：
       以 ``-`` 开头的是选项（带值选项的下一个 token 也丢掉），纯数字是选项值，其余
       按 PEP 508 截到名字为止。

    Returns:
        包名集合（大小写保持原样；本仓库全是小写）。
    """
    names: set[str] = set()
    for logical in _logical_lines(text):
        if "pip install" not in logical:
            continue
        for segment in logical.split("&&"):
            tokens = shlex.split(segment, comments=True)
            if len(tokens) < 2 or tokens[0] != "pip" or tokens[1] != "install":
                continue
            index = 2
            while index < len(tokens):
                token = tokens[index]
                index += 1
                if token.startswith("-"):
                    if token in _PIP_OPTIONS_WITH_VALUE:
                        index += 1  # 跳过它的值
                    continue
                if token.isdigit():
                    continue  # 选项值（如 ``--retries 10`` 里解析失败时的兜底）
                name = _NAME_SPLIT.split(token, 1)[0].strip()
                if name:
                    names.add(name)
    return names


def _node_baseline_packages() -> set[str]:
    """``docker/node-baseline/package.json`` 声明的包（dependencies + devDependencies）。"""
    payload = json.loads(NODE_BASELINE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        block = payload.get(section) or {}
        names |= {str(name) for name in block}
    return names


class TestImageManifestMatchesTables:
    """仓内包表必须与镜像/构建期的清单**集合相等**（双向）。"""

    def test_python_table_equals_dockerfile_pip_install(self) -> None:
        """``SANDBOX_IMAGE_PACKAGES`` == Dockerfile 里 pip 装的包集合。"""
        installed = _pip_requirements(DOCKERFILE.read_text(encoding="utf-8"))

        # 镜像有、表没登记 ⇒ 声明了也会被判"缺失"（误 skip，方向宽松但静默）
        missing_in_table = sorted(installed - set(SANDBOX_IMAGE_PACKAGES))
        assert not missing_in_table, (
            "镜像装了但包表没登记（会被误判为 requires_missing_deps ⇒ 误 skip）："
            f"{missing_in_table}"
        )
        # 表里有、镜像没装 ⇒ 检查跑起来 ImportError ⇒ 误判"代码坏了"（最坏方向）
        missing_in_image = sorted(set(SANDBOX_IMAGE_PACKAGES) - installed)
        assert not missing_in_image, (
            "包表登记了但镜像没装（检查会 ImportError ⇒ 把环境问题记成代码缺陷）："
            f"{missing_in_image}"
        )

    def test_node_table_equals_node_baseline_package_json(self) -> None:
        """``SANDBOX_IMAGE_NODE_PACKAGES`` == ``node-baseline/package.json`` 的依赖集合。"""
        declared = _node_baseline_packages()
        assert declared == set(SANDBOX_IMAGE_NODE_PACKAGES), (
            "node 侧包表与 docker/node-baseline/package.json 不一致："
            f"镜像={sorted(declared)} 表={sorted(SANDBOX_IMAGE_NODE_PACKAGES)}"
        )

    def test_tables_are_selectable_by_language(self) -> None:
        """两张表都能被 ``image_packages_for`` 选到（校验 4/25 的入口）。"""
        assert image_packages_for("python") == SANDBOX_IMAGE_PACKAGES
        assert image_packages_for("typescript") == SANDBOX_IMAGE_NODE_PACKAGES
        assert image_packages_for("cobol") == frozenset()

    def test_parser_sanity(self) -> None:
        """解析器自身的正反例（防"两个集合都空 ⇒ 断言恒真"）。"""
        sample = (
            'pip install --no-cache-dir --retries 10 --timeout 120 "pytest>=8.2,<9" httpx \\\n'
            '  && echo done\n'
        )
        assert _pip_requirements(sample) == {"pytest", "httpx"}
        # 非 pip 行不得贡献任何包
        assert _pip_requirements("apt-get install -y curl") == set()
