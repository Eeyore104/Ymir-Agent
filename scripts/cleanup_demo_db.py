"""演示库遗留数据清理（T5-5⑤）。

直接操作 SQLite 删除测试项目（及联动的 run / 阶段 / 消息 / 事件 / 审批 / 用量 /
沙箱会话 / 产物元数据），并清理 ``data/artifacts/<project_id>/`` 文件目录。

用法（仓库根目录）：

  # 预览将删除的项目（不落盘）
  uv run python scripts/cleanup_demo_db.py --project-name 演示 --dry-run

  # 精确删除某项目名
  uv run python scripts/cleanup_demo_db.py --project-name "单跑演示-1015" --yes

  # 按 ID 删除
  uv run python scripts/cleanup_demo_db.py --project-id <uuid> --yes

  # 删除名字包含某关键字的全部项目（危险，建议先 --dry-run）
  uv run python scripts/cleanup_demo_db.py --contains 演示 --yes
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from ymir.config import get_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ymir-agent 演示库清理（直接操作 SQLite）")
    parser.add_argument("--project-id", default=None, help="按项目 ID 精确删除")
    parser.add_argument("--project-name", default=None, help="按项目名精确删除")
    parser.add_argument("--contains", default=None, help="删除项目名包含该关键字的项目（模糊）")
    parser.add_argument("--yes", action="store_true", help="确认执行（缺省仅预览）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不落盘")
    return parser.parse_args()


def find_projects(
    conn: sqlite3.Connection, args: argparse.Namespace
) -> list[tuple[str, str]]:
    """按条件查找目标项目（返回 ``[(id, name), ...]``）。"""
    if args.project_id:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE id = ?", (args.project_id,)
        ).fetchall()
    elif args.project_name:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE name = ?", (args.project_name,)
        ).fetchall()
    elif args.contains:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE name LIKE ?", (f"%{args.contains}%",)
        ).fetchall()
    else:
        rows = []
    return [(str(row[0]), str(row[1])) for row in rows]


def counts(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
    """统计项目联动数据量（供预览）。"""

    def _count(sql: str) -> int:
        return int(conn.execute(sql, (project_id,)).fetchone()[0])

    run_sub = "(SELECT id FROM pipeline_runs WHERE project_id = ?)"
    return {
        "runs": _count("SELECT count(*) FROM pipeline_runs WHERE project_id = ?"),
        "artifacts": _count("SELECT count(*) FROM artifacts WHERE project_id = ?"),
        "messages": _count(f"SELECT count(*) FROM messages WHERE run_id IN {run_sub}"),
        "events": _count(f"SELECT count(*) FROM events WHERE run_id IN {run_sub}"),
        "approvals": _count(f"SELECT count(*) FROM approvals WHERE run_id IN {run_sub}"),
    }


def main() -> int:
    args = parse_args()
    settings = get_settings()
    db_path = settings.resolved_db_path
    if not Path(db_path).exists():
        print(f"[cleanup] 数据库不存在：{db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        targets = find_projects(conn, args)
        if not targets:
            print("[cleanup] 未匹配到任何项目（检查 --project-name / --contains / --project-id）。")
            return 0
        print(f"[cleanup] 数据库：{db_path}")
        print(f"[cleanup] 匹配到 {len(targets)} 个项目：")
        for project_id, name in targets:
            stat = counts(conn, project_id)
            print(
                f"  - {name}（{project_id}）："
                f"runs={stat['runs']} artifacts={stat['artifacts']} "
                f"messages={stat['messages']} events={stat['events']} approvals={stat['approvals']}"
            )

        if args.dry_run or not args.yes:
            print("[cleanup] 预览模式（未删除）。确认执行请追加 --yes")
            return 0

        for project_id, _name in targets:
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            artifact_dir = Path(settings.resolved_artifacts_dir) / project_id
            if artifact_dir.is_dir():
                shutil.rmtree(artifact_dir, ignore_errors=True)
        conn.commit()
        print(f"[cleanup] 已删除 {len(targets)} 个项目及其联动数据与产物目录。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
