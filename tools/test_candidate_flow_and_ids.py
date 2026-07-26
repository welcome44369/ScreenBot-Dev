"""Regression coverage for OCR-candidate flow, corrections, and generated IDs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.trigger_store import TriggerStore
from app.trigger_wizard import TriggerWizard
from app.workflow_manager import WorkflowManager
from app.workflow_store import WorkflowStore


APP = QApplication.instance() or QApplication([])
REGION = {
    "x_ratio": 0.1,
    "y_ratio": 0.1,
    "width_ratio": 0.5,
    "height_ratio": 0.5,
}


def pipeline_result(*texts):
    return {
        "clean_text": "\n".join(texts),
        "candidate_metadata": [
            {
                "text": text,
                "language": "chi_tra",
                "pipeline": "chinese/test/chi_tra/psm6",
                "psm": 6,
                "confidence": 90.0,
                "bounding_box": (0, 0, 100, 20),
                "line_id": "1:1:1",
                "support_count": 2,
                "sources": [],
            }
            for text in texts
        ],
    }


class CandidateFlowAndIdTest(unittest.TestCase):
    def test_reocr_preserves_correction_and_merges_exact_match(self):
        with tempfile.TemporaryDirectory() as directory:
            wizard = TriggerWizard(TriggerStore(directory), None, None)
            wizard._merge_ocr_candidates(pipeline_result("家人的餐"))
            item = wizard.candidates.item(0)
            item.setText("家人的餐廳")
            metadata = wizard._item_metadata(item)
            metadata.update(
                {
                    "source": "user_corrected",
                    "original_text": "家人的餐",
                    "text": "家人的餐廳",
                }
            )
            item.setData(Qt.ItemDataRole.UserRole, metadata)
            item.setCheckState(Qt.CheckState.Checked)

            wizard._merge_ocr_candidates(pipeline_result("家人的餐"))
            self.assertEqual(wizard.candidates.count(), 2)
            self.assertEqual(wizard.candidates.item(1).text(), "家人的餐廳")
            self.assertEqual(
                wizard._item_metadata(wizard.candidates.item(1))["source"],
                "user_corrected",
            )

            wizard._merge_ocr_candidates(pipeline_result("家人的餐廳"))
            self.assertEqual(wizard.candidates.count(), 1)
            merged = wizard.candidates.item(0)
            self.assertEqual(merged.text(), "家人的餐廳")
            self.assertEqual(wizard._item_metadata(merged)["source"], "user_corrected")
            self.assertEqual(merged.checkState(), Qt.CheckState.Checked)
            self.assertFalse(hasattr(wizard, "add_text"))
            self.assertFalse(hasattr(wizard, "id_edit"))
            self.assertNotIn(
                "新增文字",
                [button.text() for button in wizard.findChildren(QPushButton)],
            )
            wizard.close()

    def test_correction_case_is_created_only_after_successful_wizard_save(self):
        with tempfile.TemporaryDirectory() as directory:
            trigger_store = TriggerStore(directory)
            cancelled = TriggerWizard(trigger_store, None, None)
            cancelled.reject()
            self.assertFalse((Path(directory) / "ocr-corrections").exists())

            wizard = TriggerWizard(trigger_store, None, None)
            wizard.name_edit.setText("修正後 Trigger")
            wizard.region = REGION
            wizard._last_crop = Image.new("RGB", (12, 8), "white")
            wizard._last_pipeline_result = pipeline_result("家人的餐")
            wizard._merge_ocr_candidates(wizard._last_pipeline_result)
            item = wizard.candidates.item(0)
            item.setText("家人的餐廳")
            metadata = wizard._item_metadata(item)
            metadata.update(
                {
                    "source": "user_corrected",
                    "original_text": "家人的餐",
                    "text": "家人的餐廳",
                }
            )
            item.setData(Qt.ItemDataRole.UserRole, metadata)
            item.setCheckState(Qt.CheckState.Checked)
            wizard.save()

            cases = list((Path(directory) / "ocr-corrections").iterdir())
            self.assertEqual(len(cases), 1)
            case = cases[0]
            self.assertTrue((case / "crop-original.png").exists())
            self.assertTrue((case / "pipeline-result.json").exists())
            correction = json.loads((case / "correction.json").read_text(encoding="utf-8"))
            self.assertRegex(correction["trigger_id"], r"^trigger_[0-9a-f]{32}$")
            self.assertEqual(correction["corrections"][0]["text"], "家人的餐廳")
            trigger = trigger_store.load_trigger(correction["trigger_id"])
            self.assertEqual(trigger["texts"], ["家人的餐廳"])
            self.assertNotIn("corrections", trigger)
            self.assertNotIn("pipeline_metadata", trigger)

    def test_trigger_ids_are_generated_and_legacy_ids_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TriggerStore(directory)
            payload = {
                "version": 1,
                "name": "自動 ID",
                "type": "text",
                "event": "appear",
                "texts": ["可樂"],
                "region": REGION,
            }
            trigger_id = store.save_trigger(payload)
            self.assertRegex(trigger_id, r"^trigger_[0-9a-f]{32}$")
            data = store.load_trigger(trigger_id)
            data["name"] = "改名不改 ID"
            store.update_trigger(trigger_id, data)
            self.assertEqual(store.load_trigger(trigger_id)["id"], trigger_id)

            legacy = dict(payload, id="cola_trigger", name="舊 Trigger")
            store.save_trigger(legacy)
            self.assertEqual(store.load_trigger("cola_trigger")["id"], "cola_trigger")

            no_id_path = Path(directory) / "triggers" / "missing-id.json"
            no_id_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            loaded = store.load_trigger("missing-id")
            self.assertRegex(loaded["id"], r"^trigger_[0-9a-f]{32}$")

    def test_workflow_ids_are_generated_and_legacy_workflow_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStore(directory)
            workflow = {
                "version": 2,
                "name": "自動 Workflow ID",
                "steps": [{"id": "step_1", "trigger_ref": "t", "macro_ref": "m"}],
            }
            filename = store.save_workflow(workflow)
            saved = store.load_workflow(filename)
            self.assertRegex(saved["id"], r"^workflow_[0-9a-f]{32}$")
            saved["name"] = "改名不改 Workflow ID"
            store.save_workflow(saved, filename)
            self.assertEqual(store.load_workflow(filename)["id"], saved["id"])

            legacy = dict(workflow, name="Legacy Workflow")
            legacy_path = Path(directory) / "workflows" / "legacy.json"
            legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(store.load_workflow("legacy.json")["name"], "Legacy Workflow")

    def test_workflow_manager_hides_id_and_delegates_generation_to_store(self):
        class MacroStore:
            @staticmethod
            def list_scripts():
                return [("test_macro.json", "測試巨集")]

        with tempfile.TemporaryDirectory() as directory:
            trigger_store = TriggerStore(directory)
            trigger_store.save_trigger(
                {
                    "version": 1,
                    "name": "測試觸發點",
                    "type": "text",
                    "event": "appear",
                    "texts": ["可樂"],
                    "region": REGION,
                }
            )
            workflow_store = WorkflowStore(directory)
            manager = WorkflowManager(workflow_store, trigger_store, MacroStore())
            self.assertFalse(hasattr(manager, "id_edit"))
            manager.name_edit.setText("免填 ID 工作流")
            manager._save()
            items = workflow_store.list_workflows()
            self.assertEqual(len(items), 1)
            self.assertRegex(
                workflow_store.load_workflow(items[0]["filename"])["id"],
                r"^workflow_[0-9a-f]{32}$",
            )
            manager.close()


if __name__ == "__main__":
    unittest.main()
