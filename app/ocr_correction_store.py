"""Persist OCR cases only after a Trigger has been saved successfully."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


class OCRCorrectionStore:
    """Stores immutable OCR evidence for later regression analysis."""

    def __init__(self, root_path):
        self.root_path = Path(root_path)
        self.corrections_dir = self.root_path / "ocr-corrections"

    def record(self, *, crop_image, pipeline_result, corrections, trigger_id):
        """Write one completed OCR case after its Trigger has been persisted."""
        case_id = f"ocr_case_{uuid4().hex}"
        case_dir = self.corrections_dir / case_id
        case_dir.mkdir(parents=True, exist_ok=False)
        crop_image.save(case_dir / "crop-original.png")
        (case_dir / "pipeline-result.json").write_text(
            json.dumps(pipeline_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        correction = {
            "version": 1,
            "case_id": case_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "trigger_id": trigger_id,
            "corrections": corrections,
        }
        (case_dir / "correction.json").write_text(
            json.dumps(correction, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return case_dir
