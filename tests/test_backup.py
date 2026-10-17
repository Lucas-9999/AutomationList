"""All backup acceptance data below is synthetic; no customer files are copied."""
import hashlib
import json
import stat
import subprocess
import sys
import warnings
import zipfile

import pytest

from tradeflow.backup import create_backup, restore_backup, verify_backup
from tradeflow.store import Store


def seed(root):
    store = Store(root)
    digest = store.save_original(b"SYNTHETIC inquiry raw bytes")
    template = store.save_original(b"SYNTHETIC template bytes")
    inquiry = store.create_record("inquiry", "SYNTHETIC", {"sha256": digest, "body": "SYNTHETIC"})
    allocation = store.create_record("allocation", "SYNTHETIC allocation", {"source_sha256": digest, "quantities": {"SYNTHETIC-L1": "100"}})
    store.create_record("draft", "SYNTHETIC draft", {"template_sha256": template, "sha256": digest})
    workflow = store.create_workflow(inquiry["id"])
    # Unreferenced files, configuration files and environment values are excluded.
    store.save_original(b"SYNTHETIC unreferenced")
    (root / ".env").write_text("SYNTHETIC_NOT_A_REAL_KEY=excluded", encoding="utf-8")
    return store, digest, inquiry, allocation, workflow


def rewrite(source, target, change):
    with zipfile.ZipFile(source) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    change(parts)
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def test_roundtrip_preserves_records_audit_allocations_and_raw(tmp_path):
    store, digest, inquiry, allocation, workflow = seed(tmp_path / "source")
    original_counts = store.history(inquiry["id"])
    target = tmp_path / "synthetic-backup.zip"
    report = create_backup(store.root, target)
    assert report["verified"] is True
    assert report["raw_files"] == 2
    assert report["counts"] == {"records": 3, "audit": 4, "workflows": 1, "allocations": 1}
    assert verify_backup(target)["counts"] == report["counts"]
    with zipfile.ZipFile(target) as archive:
        assert len(archive.namelist()) == 4
        assert all("env" not in name for name in archive.namelist())
    restored_path = tmp_path / "restored"
    restore_backup(target, restored_path)
    restored = Store(restored_path)
    assert restored.get(inquiry["id"]) == inquiry
    assert restored.get(allocation["id"]) == allocation
    assert restored.workflow(workflow["id"]) == workflow
    assert restored.history(inquiry["id"]) == original_counts
    assert (restored_path / "raw" / digest).read_bytes() == b"SYNTHETIC inquiry raw bytes"
    assert store.history(inquiry["id"]) == original_counts


def test_snapshot_includes_committed_wal_rows(tmp_path):
    store, _, _, _, _ = seed(tmp_path / "source")
    # Hold a live connection to keep a committed WAL transaction outstanding.
    with store.connect() as connection:
        connection.execute("INSERT INTO audit(entity_id,action,detail,created_at) VALUES (?,?,?,?)", ("SYNTHETIC", "wal", "{}", "synthetic"))
        connection.commit()
        report = create_backup(store.root, tmp_path / "wal.zip")
        assert report["counts"]["audit"] == 5


@pytest.mark.parametrize("mode", ["changed", "missing", "changed_manifest"])
def test_tampered_or_missing_part_rejected_before_restore(tmp_path, mode):
    store, digest, *_ = seed(tmp_path / "source")
    original = tmp_path / "good.zip"
    create_backup(store.root, original)

    def change(parts):
        name = "raw/" + digest
        if mode == "changed":
            parts[name] += b"TAMPERED"
        elif mode == "missing":
            del parts[name]
        else:
            manifest = json.loads(parts["manifest.json"])
            manifest["parts"][name]["sha256"] = "0" * 64
            parts["manifest.json"] = json.dumps(manifest).encode()

    damaged = tmp_path / "damaged.zip"
    rewrite(original, damaged, change)
    with pytest.raises(ValueError):
        verify_backup(damaged)
    destination = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        restore_backup(damaged, destination)
    assert not destination.exists()


def test_missing_source_original_and_existing_destinations_rejected(tmp_path):
    store, digest, *_ = seed(tmp_path / "source")
    backup = tmp_path / "good.zip"
    create_backup(store.root, backup)
    before = backup.read_bytes()
    with pytest.raises(FileExistsError):
        create_backup(store.root, backup)
    assert backup.read_bytes() == before
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        restore_backup(backup, existing)
    (store.root / "raw" / digest).unlink()
    missing = tmp_path / "missing.zip"
    with pytest.raises(ValueError, match="原件缺失"):
        create_backup(store.root, missing)
    assert not missing.exists()


@pytest.mark.parametrize("name", ["../escape", "raw/../escape", "C:/escape", "raw\\escape", "/escape"])
def test_zip_path_traversal_rejected(tmp_path, name):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr(name, b"SYNTHETIC")
    with pytest.raises(ValueError):
        verify_backup(archive)


def test_duplicate_zip_and_duplicate_manifest_keys_rejected(tmp_path):
    duplicate = tmp_path / "duplicate.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("manifest.json", "{}")
            archive.writestr("manifest.json", "{}")
    with pytest.raises(ValueError, match="ZIP 路径"):
        verify_backup(duplicate)
    store, *_ = seed(tmp_path / "source")
    good = tmp_path / "good.zip"
    create_backup(store.root, good)
    bad = tmp_path / "duplicate-manifest.zip"
    rewrite(good, bad, lambda parts: parts.update({"manifest.json": b'{"format":"x","format":"y"}'}))
    with pytest.raises(ValueError, match="重复键"):
        verify_backup(bad)


def test_database_reference_validation_not_just_manifest_hashes(tmp_path):
    store, digest, *_ = seed(tmp_path / "source")
    good = tmp_path / "good.zip"
    create_backup(store.root, good)

    def remove_reference_part_and_manifest(parts):
        del parts["raw/" + digest]
        manifest = json.loads(parts["manifest.json"])
        del manifest["parts"]["raw/" + digest]
        manifest["raw_sha256"].remove(digest)
        parts["manifest.json"] = json.dumps(manifest).encode()

    bad = tmp_path / "missing-reference.zip"
    rewrite(good, bad, remove_reference_part_and_manifest)
    with pytest.raises(ValueError, match="数据库原件引用"):
        verify_backup(bad)


def test_database_integrity_even_with_rehashed_manifest(tmp_path):
    store, *_ = seed(tmp_path / "source")
    good = tmp_path / "good.zip"
    create_backup(store.root, good)

    def corrupt(parts):
        parts["tradeflow.sqlite3"] = b"SYNTHETIC invalid database"
        manifest = json.loads(parts["manifest.json"])
        manifest["parts"]["tradeflow.sqlite3"] = {"sha256": hashlib.sha256(parts["tradeflow.sqlite3"]).hexdigest(), "size": len(parts["tradeflow.sqlite3"])}
        parts["manifest.json"] = json.dumps(manifest).encode()

    bad = tmp_path / "bad-db.zip"
    rewrite(good, bad, corrupt)
    with pytest.raises(ValueError):
        verify_backup(bad)


def test_symbolic_target_rejected_when_platform_allows_symlinks(tmp_path):
    store, *_ = seed(tmp_path / "source")
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("此 Windows 会话未授予创建符号链接权限")
    with pytest.raises(ValueError, match="符号链接"):
        create_backup(store.root, link / "backup.zip")


def test_email_attachment_hash_is_metadata_not_separate_raw_reference(tmp_path):
    from email.message import EmailMessage
    from tradeflow.ingestion import parse_email

    email = EmailMessage()
    email["Subject"] = "SYNTHETIC attachment inquiry"
    email.set_content("SYNTHETIC inquiry")
    email.add_attachment(b"SYNTHETIC attachment bytes", maintype="application", subtype="octet-stream", filename="synthetic.bin")
    content = email.as_bytes()
    parsed = parse_email(content, "synthetic.eml")
    store = Store(tmp_path / "source")
    parsed["sha256"] = store.save_original(content)
    record = store.create_record("inquiry", "SYNTHETIC attached email", parsed)
    backup = tmp_path / "attached-email.zip"
    report = create_backup(store.root, backup)
    assert report["raw_files"] == 1
    restored_path = tmp_path / "restored"
    restore_backup(backup, restored_path)
    restored = Store(restored_path)
    assert restored.get(record["id"]) == record
    assert (restored_path / "raw" / parsed["sha256"]).read_bytes() == content


def test_zip_symlink_member_rejected_without_os_symlink_permission(tmp_path):
    backup = tmp_path / "symlink.zip"
    member = zipfile.ZipInfo("tradeflow.sqlite3")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr(member, b"elsewhere")
    with pytest.raises(ValueError, match="ZIP 路径"):
        verify_backup(backup)


def test_size_limit_checked_before_database_extraction(tmp_path, monkeypatch):
    import tradeflow.backup as module

    backup = tmp_path / "oversized.zip"
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr("tradeflow.sqlite3", b"SYNTHETIC" * 20)
    monkeypatch.setattr(module, "MAX_PART", 16)
    with pytest.raises(ValueError, match="容量限制"):
        verify_backup(backup)


def test_cli_create_verify_restore_new_and_reject_overwrite(tmp_path):
    from pathlib import Path

    store, *_ = seed(tmp_path / "source")
    cli = Path(__file__).resolve().parents[1] / "tools" / "backup_data.py"
    backup = tmp_path / "cli.zip"
    destination = tmp_path / "restored-cli"
    commands = [
        ["create", "--data-root", str(store.root), "--destination", str(backup)],
        ["verify", "--backup", str(backup)],
        ["restore-new", "--backup", str(backup), "--new-root", str(destination)],
    ]
    for command in commands:
        result = subprocess.run([sys.executable, str(cli), *command], capture_output=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["ok"] is True
    result = subprocess.run([sys.executable, str(cli), *commands[-1]], capture_output=True)
    assert result.returncode == 1
    assert json.loads(result.stderr)["ok"] is False
