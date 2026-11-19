"""Clip Finder and Video Editor agents inside the idea pipeline.

The heavy work happens in Video Studio jobs. In an idea's pipeline these agents confirm and report
what Studio produced for that idea, or hand the step to a person.
"""

from __future__ import annotations

from .base import Agent, AgentResult
from .registry import SPECS


class ClipFinderAgent(Agent):
    spec = SPECS["clip_finder"]

    def run(self, conn, task: dict, idea: dict) -> AgentResult:
        from ..studio.service import draft_for_idea, get_clip

        d = draft_for_idea(conn, idea["id"])
        if d is None:
            return AgentResult(False, "No Video Studio clip is linked to this idea. Upload the recording in Video "
                               "Studio, or find the footage by hand and mark this done.", needs_human=True)
        c = get_clip(conn, d["clip_id"])
        flags = "; ".join(c["context_flags"]) or "none"
        return AgentResult(True, f"Clip from '{d['recording_title']}' {c['start']:.1f}s–{c['end']:.1f}s "
                                 f"({c['duration']:.0f}s), score {c['score']}. Context flags: {flags}. "
                                 f"Footage: {'third-party (permission checked)' if d['ownership'] == 'third_party' else 'own'}.")


class VideoEditorAgent(Agent):
    spec = SPECS["video_editor"]

    def run(self, conn, task: dict, idea: dict) -> AgentResult:
        from ..studio.service import draft_for_idea

        d = draft_for_idea(conn, idea["id"])
        if d is None:
            return AgentResult(False, "No Video Studio draft is linked. Render one in Video Studio, or edit by hand "
                               "and mark this done.", needs_human=True)
        if d["status"] in ("queued", "rendering", "qc"):
            return AgentResult(False, f"Draft #{d['id']} is still {d['status']}.", waiting=True)
        if d["status"] != "approved":
            return AgentResult(False, f"Draft #{d['id']} is '{d['status']}'. Approve it in Video Studio's approval "
                                      "queue (or edit by hand and mark this done).", needs_human=True)
        return AgentResult(True, f"Draft #{d['id']} v{d['version']}: {d['width']}x{d['height']}, {d['duration']:.1f}s, "
                                 f"QC {d['qc_status']}, approved by {d['approved_by']}.")
