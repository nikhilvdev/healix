"""Small filesystem helpers shared across Healix."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: str | os.PathLike[str], data: Any, *, indent: int = 2) -> None:
    """Write ``data`` as JSON to ``path`` atomically (temp file + rename).

    A crash mid-write never leaves a truncated file: readers see either the old
    contents or the new, never a partial one. Parent directories are created.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
