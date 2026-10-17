"""Consistent, verifiable local backups; never overwrites a destination."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime, timezone
import zipfile

FORMAT = "tradeflow-local-backup"
MAX_PART = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_ENTRIES = 10000
HASH = re.compile(r"[0-9a-f]{64}\Z")


def _safe_path(path: Path) -> Path:
    # Do not resolve first: that would hide a symlink or Windows junction.
    path = Path(os.path.abspath(path))
    for node in (path, *path.parents):
        if node.is_symlink() or (hasattr(node, "is_junction") and node.is_junction()):
            raise ValueError("路径包含符号链接或目录联接")
    return path


def _digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_PART:
                raise ValueError("备份部件超过大小限制")
            digest.update(chunk)
    return digest.hexdigest(), size


def _readonly(db: Path):
    con = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
    con.execute("PRAGMA trusted_schema=OFF")
    con.execute("PRAGMA query_only=ON")
    return con


def _references(db: Path) -> tuple[set[str], dict]:
    refs: set[str] = set()

    def visit(value):
        # Only controlled record-level references point to raw files. Nested
        # attachment hashes describe bytes retained inside the original EML.
        for key in ("sha256", "template_sha256", "source_sha256"):
            item = value.get(key)
            if item is not None:
                if not isinstance(item, str) or not HASH.fullmatch(item):
                    raise ValueError("数据库原件哈希格式无效")
                refs.add(item)

    with closing(_readonly(db)) as con:
        if con.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("数据库完整性检查失败")
        schema = con.execute("SELECT type,name,sql FROM sqlite_master").fetchall()
        if any(kind == "trigger" or (sql and "CREATE VIRTUAL TABLE" in sql.upper()) for kind, _, sql in schema):
            raise ValueError("备份数据库包含不支持的可执行结构")
        tables = {name for kind, name, _ in schema if kind == "table"}
        if not {"records", "audit", "workflows"} <= tables:
            raise ValueError("备份数据库缺少业务表")
        counts = {"records": 0, "audit": 0, "workflows": 0, "allocations": 0}
        for kind, payload in con.execute("SELECT kind,payload FROM records"):
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise ValueError("业务记录 payload 必须是对象")
            visit(value)
            counts["records"] += 1
            counts["allocations"] += kind == "allocation"
        # Ensure these tables and their expected fields are readable too.
        for payload, in con.execute("SELECT payload FROM workflows"):
            if not isinstance(json.loads(payload), dict):
                raise ValueError("流程 payload 必须是对象")
            counts["workflows"] += 1
        for detail, in con.execute("SELECT detail FROM audit"):
            json.loads(detail)
            counts["audit"] += 1
    return refs, counts


def create_backup(data_root: Path, destination: Path) -> dict:
    """Create one new ZIP from a SQLite backup snapshot and referenced raw files."""
    source = _safe_path(data_root)
    target = _safe_path(destination)
    if not source.is_dir() or not (source / "tradeflow.sqlite3").is_file():
        raise ValueError("数据目录或数据库不存在")
    _safe_path(source / "tradeflow.sqlite3")
    if target.exists():
        raise FileExistsError("备份目标已存在，禁止覆盖")
    if not target.parent.is_dir():
        raise ValueError("备份目标父目录必须已存在")
    if target.is_relative_to(source):
        raise ValueError("备份目标必须在源数据目录之外")
    with tempfile.TemporaryDirectory(prefix="tradeflow-backup-") as scratch:
        snapshot = Path(scratch) / "tradeflow.sqlite3"
        # Do not use immutable here: committed data may still live in WAL.
        with closing(sqlite3.connect((source / "tradeflow.sqlite3").as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
        refs, counts = _references(snapshot)
        parts = {"tradeflow.sqlite3": snapshot}
        for digest in sorted(refs):
            original = _safe_path(source / "raw" / digest)
            if not original.is_file():
                raise ValueError("数据库引用的原件缺失")
            parts["raw/" + digest] = original
        entries = {}
        total = 0
        for name, path in parts.items():
            digest, size = _digest(path)
            if name.startswith("raw/") and digest != path.name:
                raise ValueError("原件内容与文件名哈希不一致")
            entries[name] = {"sha256": digest, "size": size}
            total += size
        if total > MAX_TOTAL or len(parts) + 1 > MAX_ENTRIES:
            raise ValueError("备份超过容量限制")
        manifest = {"format": FORMAT, "version": 1,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "parts": entries, "raw_sha256": sorted(refs)}
        with target.open("xb") as handle:
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True))
                for name, path in parts.items():
                    archive.write(path, name)
        # Detect a raw-file change between preflight hashing and ZIP writing.
        report = verify_backup(target)
        return {**report, "backup": str(target), "counts": counts}


def _json_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("manifest 包含重复键")
        result[key] = value
    return result


def _validate(archive: zipfile.ZipFile, scratch: Path) -> dict:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ENTRIES:
        raise ValueError("备份条目数量无效")
    names = set()
    total = 0
    for info in infos:
        name = info.filename
        pure = PurePosixPath(name)
        if (name in names or "\\" in name or pure.is_absolute() or ":" in name
                or any(x in {"", ".", ".."} for x in name.split("/"))
                or str(pure) != name or info.is_dir()
                or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
            raise ValueError("备份包含重复或不安全的 ZIP 路径")
        if name not in {"manifest.json", "tradeflow.sqlite3"} and not (name.startswith("raw/") and HASH.fullmatch(name[4:])):
            raise ValueError("备份包含未允许的部件")
        names.add(name)
        total += info.file_size
        if info.file_size > MAX_PART or total > MAX_TOTAL:
            raise ValueError("备份超过容量限制")
    if not {"manifest.json", "tradeflow.sqlite3"} <= names:
        raise ValueError("备份缺少 manifest 或数据库")
    if archive.getinfo("manifest.json").file_size > 4 * 1024 * 1024:
        raise ValueError("manifest 过大")
    manifest = json.loads(archive.read("manifest.json"), object_pairs_hook=_json_unique)
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or type(manifest.get("version")) is not int or manifest["version"] != 1:
        raise ValueError("备份格式或版本无效")
    parts = manifest.get("parts")
    if not isinstance(parts, dict) or set(parts) != names - {"manifest.json"}:
        raise ValueError("manifest 部件清单不一致")
    for name, spec in parts.items():
        if (not isinstance(spec, dict) or set(spec) != {"sha256", "size"}
                or not isinstance(spec["sha256"], str) or not HASH.fullmatch(spec["sha256"])
                or type(spec["size"]) is not int or spec["size"] != archive.getinfo(name).file_size):
            raise ValueError("manifest 部件属性无效")
        digest = hashlib.sha256()
        # Only the database is written, into an isolated new temporary directory.
        db_file = (scratch / "tradeflow.sqlite3").open("xb") if name == "tradeflow.sqlite3" else None
        try:
            with archive.open(name) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    if db_file:
                        db_file.write(chunk)
        finally:
            if db_file:
                db_file.close()
        if digest.hexdigest() != spec["sha256"] or (name.startswith("raw/") and digest.hexdigest() != name[4:]):
            raise ValueError("备份部件哈希校验失败")
    refs, counts = _references(scratch / "tradeflow.sqlite3")
    listed_refs = manifest.get("raw_sha256")
    if not isinstance(listed_refs, list) or listed_refs != sorted(refs) or {"raw/" + x for x in refs} != names - {"manifest.json", "tradeflow.sqlite3"}:
        raise ValueError("数据库原件引用与备份清单不一致或原件缺失")
    return {"verified": True, "format_version": 1, "raw_files": len(refs), "parts": len(parts), "counts": counts}


def verify_backup(path: Path) -> dict:
    source = _safe_path(path)
    try:
        with tempfile.TemporaryDirectory(prefix="tradeflow-verify-") as scratch:
            with zipfile.ZipFile(source, "r") as archive:
                return _validate(archive, Path(scratch))
    except (zipfile.BadZipFile, sqlite3.DatabaseError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("备份内容损坏或格式无效") from error


def restore_backup(path: Path, new_root: Path) -> dict:
    """Fully verify first, then restore into a brand-new directory only."""
    source = _safe_path(path)
    destination = _safe_path(new_root)
    if destination.exists():
        raise FileExistsError("恢复目标已存在；请选择全新目录")
    if not destination.parent.is_dir():
        raise ValueError("恢复目标父目录必须已存在")
    try:
        # Keep this same archive handle for validation and copying (no path reopen).
        with tempfile.TemporaryDirectory(prefix="tradeflow-restore-") as scratch:
            with zipfile.ZipFile(source, "r") as archive:
                report = _validate(archive, Path(scratch))
                _safe_path(destination)
                destination.mkdir(exist_ok=False)
                (destination / "raw").mkdir(exist_ok=False)
                # Explicit allowlisted members; never call extract/extractall.
                for info in archive.infolist():
                    if info.filename == "manifest.json":
                        continue
                    target = _safe_path(destination / info.filename)
                    with archive.open(info) as incoming, target.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        return {**report, "restored_to": str(destination)}
    except (zipfile.BadZipFile, sqlite3.DatabaseError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("备份内容损坏或格式无效；若已建立目标目录，请保留现场核对") from error
