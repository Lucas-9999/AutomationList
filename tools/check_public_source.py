"""Fail closed on private file types, real configuration files and common secrets.

This is a publishing guard, not a guarantee of absence of all sensitive data.
Use --tracked for exactly the Git index; diagnostics never show matching values.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

PRIVATE_DIRS = {'data', 'inputs', 'outputs', 'backups', 'private', 'private_fixtures', 'tmp', '.venv', '__pycache__', '.pytest_cache', 'node_modules'}
PRIVATE_SUFFIXES = {'.xlsx', '.xls', '.docx', '.doc', '.pdf', '.eml', '.sqlite3', '.db', '.zip', '.pem', '.key', '.pfx', '.log'}
TEXT_SUFFIXES = {'.py', '.js', '.cjs', '.html', '.md', '.txt', '.json', '.yml', '.yaml', '.ps1', '.cmd', '.toml'}
TOKEN_PATTERNS = [
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{30,}\b'),
    re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b'),
    re.compile(r'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
]


def inspect_file(root: Path, relative: Path) -> list[str]:
    if relative.is_absolute() or '..' in relative.parts:
        return ['unsafe_path']
    path = root / relative
    if path.is_symlink() or not path.is_file():
        return ['not_a_regular_file']
    name = relative.name.lower()
    problems = []
    if set(relative.parts) & PRIVATE_DIRS or relative.suffix.lower() in PRIVATE_SUFFIXES:
        problems.append('private_file_or_directory')
    if (name.startswith(('.env', '.evn')) and name != '.env.example') or '.sqlite3-' in name:
        problems.append('local_configuration_or_database')
    if relative.suffix.lower() not in TEXT_SUFFIXES and name not in {'.gitignore', '.gitattributes', '.env.example'}:
        problems.append('unapproved_file_type')
    if path.stat().st_size > 2 * 1024 * 1024:
        return problems + ['oversize_source_file']
    try:
        content = path.read_text(encoding='utf-8-sig')
    except (UnicodeError, OSError):
        return problems + ['not_readable_utf8_source']
    for number, line in enumerate(content.splitlines(), 1):
        if any(pattern.search(line) for pattern in TOKEN_PATTERNS):
            problems.append(f'credential_pattern_at_line_{number}')
        if name == '.env.example' and re.match(r'\s*[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S', line):
            problems.append(f'nonempty_credential_example_at_line_{number}')
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tracked', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.tracked:
        result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True)
        files = [Path(name.decode('utf-8')) for name in result.stdout.split(b'\0') if name]
    else:
        files = [path.relative_to(root) for path in root.rglob('*') if path.is_file()
                 and not set(path.relative_to(root).parts) & (PRIVATE_DIRS | {'.git'})]
    issues = [(path, issue) for path in files for issue in inspect_file(root, path)]
    for path, issue in issues:
        print(f'{path.as_posix()}: {issue}')
    print(f'Public source check: {len(files)} files, {len(issues)} findings')
    return int(bool(issues))


if __name__ == '__main__':
    raise SystemExit(main())
