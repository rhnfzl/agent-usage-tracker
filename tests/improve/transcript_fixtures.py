import os
from pathlib import Path


def write_completed_transcript(
    path: Path, content: str, *, completed_at: float = 1_000_000_000
) -> None:
    path.write_text(content, encoding="utf-8")
    os.utime(path, (completed_at, completed_at))
