"""Where media files live.

Everything is stored on the computer running the app, under ``data/media`` inside the project
(or ``$ONTHEROADAI_MEDIA``). The folder is git-ignored: recordings, audio, transcripts and drafts
are never committed. Delete a recording in the Studio to remove its files. In a temporary cloud
workspace, everything in this folder is lost when the workspace is deleted.

    data/media/
      uploads/      partial uploads (deleted when complete or abandoned)
      recordings/   <id>/source.<ext>       the original file
      work/         <id>/audio.wav           extracted audio (deleted after transcription)
      drafts/       <draft id>.mp4, .ass     rendered drafts and their caption files
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ..db import PROJECT_ROOT


def media_root() -> Path:
    root = Path(os.environ.get("ONTHEROADAI_MEDIA", PROJECT_ROOT / "data" / "media"))
    for sub in ("uploads", "recordings", "work", "drafts"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def safe_display_name(name: str, limit: int = 120) -> str:
    """Original filename for display only. Files on disk always get generated names."""
    base = os.path.basename((name or "").replace("\\", "/"))
    base = re.sub(r"[^\w.\- ()]+", "_", base).strip(" .") or "recording"
    return base[:limit]


def resolve(rel: str | None) -> Path | None:
    """Turn a stored relative path back into an absolute one, refusing anything outside the media root."""
    if not rel:
        return None
    root = media_root().resolve()
    p = (root / rel).resolve()
    if root not in p.parents and p != root:
        raise ValueError("Path escapes the media folder")
    return p


def rel(path: Path) -> str:
    return str(Path(path).resolve().relative_to(media_root().resolve()))


def recording_dir(recording_id: int) -> Path:
    d = media_root() / "recordings" / str(int(recording_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def work_dir(recording_id: int) -> Path:
    d = media_root() / "work" / str(int(recording_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def draft_path(draft_id: int, ext: str = "mp4") -> Path:
    return media_root() / "drafts" / f"draft-{int(draft_id)}.{ext}"


def remove_tree(path: Path) -> None:
    if path and path.exists():
        shutil.rmtree(path, ignore_errors=True)


def remove_file(path: Path | None) -> None:
    if path and path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def disk_usage() -> dict:
    root = media_root()
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    free = shutil.disk_usage(root).free
    return {"root": str(root), "used_bytes": total, "free_bytes": free}
