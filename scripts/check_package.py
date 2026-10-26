#!/usr/bin/env python3
"""Check package completeness locally; does not visit links or inspect devices."""
import ast
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "README.md", "SKILL.md", "VALIDATION.md", ".gitignore", "agents/openai.yaml",
    "references/intake-handover.md", "references/network.md", "references/platforms.md",
    "references/accounts-payment.md", "references/troubleshooting.md",
    "scripts/workflow.py", "scripts/check_package.py", "tests/test_workflow.py",
)


def check(root=ROOT):
    errors = []
    for name in REQUIRED:
        path = root / name
        if not path.is_file() or path.is_symlink():
            errors.append("Missing file or symbolic link: " + name)
            continue
        content = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            try:
                ast.parse(content, filename=name)
            except SyntaxError:
                errors.append("Invalid Python syntax: " + name)
        if path.suffix == ".md":
            for link in re.findall(r"\]\(([^\s)]+)\)", content):
                parsed = urlsplit(link.strip("<>"))
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = (path.parent / unquote(parsed.path)).resolve()
                if root.resolve() not in target.parents or not target.is_file():
                    errors.append("Broken local reference: " + name + " -> " + link)
    skill = root / "SKILL.md"
    if skill.is_file():
        content = skill.read_text(encoding="utf-8")
        if not content.startswith("---\n") or "\nname: claude-client-guide\n" not in content or "\ndescription: " not in content:
            errors.append("Missing Skill entry metadata")
    return errors


if __name__ == "__main__":
    issues = check()
    for issue in issues:
        print(issue)
    if not issues:
        print("PASS: package files, local document references and Python syntax")
        print("This check does not test a network, account, external website or payment.")
    sys.exit(bool(issues))
