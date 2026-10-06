from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil


ROOT = Path.cwd()
TEST_FILE = ROOT / "tests" / "test_ollama_embeddings.py"
BACKUP_DIR = ROOT / "target" / f"embedding-timeout-test-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def main() -> None:
    if not TEST_FILE.exists():
        raise SystemExit(
            "Run this script from the MindBridge project root; "
            "tests/test_ollama_embeddings.py was not found."
        )

    backup = BACKUP_DIR / "tests" / TEST_FILE.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEST_FILE, backup)

    text = TEST_FILE.read_text(encoding="utf-8")
    old = "timeout=30.0"
    new = "timeout=store.settings.embedding_timeout_seconds"
    count = text.count(old)

    if count != 2:
        raise RuntimeError(
            f"Expected exactly 2 hard-coded timeout assertions, found {count}. "
            "No file was changed."
        )

    TEST_FILE.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

    print("Embedding timeout test expectations updated.")
    print(f"Replaced occurrences: {count}")
    print(f"Backup: {backup}")
    print("Production code was not modified.")


if __name__ == "__main__":
    main()
