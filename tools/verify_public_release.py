#!/usr/bin/env python3
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BANNED = (
    "佳农", "佳沛", "Zespri", "盒马", "大润发", "永辉", "京东", "天猫",
    "小象超市", "叮咚买菜", "山姆", "Costco", "麦德龙", "QINCE365",
    "REAL_SOURCE", "liuxiaohua", "/Users/", "aurax8040", "J27-08",
)
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://\S+", re.I),
)
TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".sql", ".json", ".md", ".txt", ".example"}


def text_findings() -> list[str]:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix == ".db":
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix not in TEXT_SUFFIXES and path.name != ".env.example":
            continue
        # The map asset contains public geographic labels; it has no business data.
        if path.name == "china_100000_full.json":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in BANNED:
            if token.lower() in text.lower():
                findings.append(f"{path.relative_to(ROOT)}: banned token {token!r}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: secret-like pattern {pattern.pattern!r}")
    return findings


def database_findings() -> list[str]:
    findings: list[str] = []
    for path in sorted((ROOT / "data").glob("*.db")):
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                text_cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')
                             if "CHAR" in (r[2] or "").upper() or "TEXT" in (r[2] or "").upper()
                             or not r[2]]
                if not text_cols:
                    continue
                quoted = ",".join(f'"{c}"' for c in text_cols)
                for row in conn.execute(f'SELECT {quoted} FROM "{table}"'):
                    joined = " ".join(str(value) for value in row if value is not None)
                    for token in BANNED:
                        if token.lower() in joined.lower():
                            findings.append(f"{path.name}:{table}: banned token {token!r}")
                            break
        finally:
            conn.close()
    return findings


def repository_findings() -> list[str]:
    findings: list[str] = []
    if (ROOT / ".env").exists():
        findings.append(".env must not be present")
    for path in ROOT.rglob("*"):
        if path.is_file() and path.stat().st_size >= 100 * 1024 * 1024:
            findings.append(f"{path.relative_to(ROOT)} exceeds GitHub's 100 MB file limit")
    return findings


def main() -> int:
    findings = repository_findings() + text_findings() + database_findings()
    if findings:
        print("PUBLIC RELEASE CHECK: FAIL")
        for item in sorted(set(findings)):
            print("-", item)
        return 1
    print("PUBLIC RELEASE CHECK: PASS")
    print("- no local .env")
    print("- no file reaches 100 MB")
    print("- no banned business identifiers, local paths, or common secret patterns found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
