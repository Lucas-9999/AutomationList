"""Local backup CLI. Examples: python tools/backup_data.py --help."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradeflow.backup import create_backup, restore_backup, verify_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="外贸工作台本地备份；不覆盖已有文件，恢复仅允许全新目录")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="从 SQLite 一致快照创建全新 ZIP")
    create.add_argument("--data-root", required=True, type=Path)
    create.add_argument("--destination", required=True, type=Path)
    verify = commands.add_parser("verify", help="只验证 ZIP、manifest、哈希、数据库和原件引用")
    verify.add_argument("--backup", required=True, type=Path)
    restore = commands.add_parser("restore-new", help="验证后恢复到尚不存在的新目录")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--new-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            report = create_backup(args.data_root, args.destination)
        elif args.command == "verify":
            report = verify_backup(args.backup)
        else:
            report = restore_backup(args.backup, args.new_root)
    except (ValueError, OSError, sqlite3.DatabaseError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
