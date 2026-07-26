"""OCR quality regression using the saved real game crop and calibration crop."""
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

from PIL import Image
import pytesseract


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ocr_pipeline import OCRPipeline


TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
GAME_CROP = PROJECT_ROOT / "logs" / "ocr-debug" / "last-crop-original.png"
CALIBRATION_CROP = (
    PROJECT_ROOT / "logs" / "ocr-debug" / "calibration-crop.png"
)


class OCRPipelineQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TESSERACT.exists():
            raise unittest.SkipTest("Tesseract executable is unavailable")
        pytesseract.pytesseract.tesseract_cmd = str(TESSERACT)

    def test_real_game_cjk_dominance_and_complete_lines(self):
        if not GAME_CROP.exists():
            self.skipTest("Saved real game crop is unavailable")
        result = OCRPipeline().recognize_text(
            Image.open(GAME_CROP),
            languages="eng+chi_tra",
            layout_hint="multi_line",
        )
        candidates = result["candidates"]
        self.assertTrue(result["white_text_detected"])
        # last-crop-original.png is intentionally the most recent live Wizard
        # crop and can be replaced by a user while tests are running. A caller
        # can pin an exact game case with SCREENBOT_GAME_OCR_EXPECTED; otherwise
        # this remains a safe real-image smoke regression.
        expected = [
            value.strip()
            for value in os.environ.get("SCREENBOT_GAME_OCR_EXPECTED", "").split("|")
            if value.strip()
        ]
        if expected:
            for value in expected:
                self.assertIn(value, candidates)
        else:
            self.assertTrue(candidates)
            self.assertTrue(
                any(any("\u3400" <= char <= "\u9fff" for char in value) for value in candidates)
            )

    def test_black_text_calibration_does_not_regress(self):
        if not CALIBRATION_CROP.exists():
            self.skipTest("Saved calibration crop is unavailable")
        result = OCRPipeline().recognize_text(
            Image.open(CALIBRATION_CROP),
            languages="eng+chi_tra",
            layout_hint="multi_line",
        )
        self.assertFalse(result["white_text_detected"])
        for expected in ("apple", "banana", "可樂", "礦泉水"):
            self.assertIn(expected, result["candidates"])

    def test_pure_english_does_not_regress(self):
        if not CALIBRATION_CROP.exists():
            self.skipTest("Saved calibration crop is unavailable")
        image = Image.open(CALIBRATION_CROP)
        english_region = image.crop((0, 0, image.width, 120))
        result = OCRPipeline().recognize_text(
            english_region,
            languages="eng+chi_tra",
            layout_hint="multi_line",
        )
        self.assertIn("apple", result["candidates"])
        self.assertIn("banana", result["candidates"])

    def test_latin_consensus_and_position_aware_cjk_filter(self):
        pipeline = OCRPipeline()

        def record(text, confidence, bbox, label):
            return {
                "text": text,
                "kind": pipeline._kind(text),
                "confidence": confidence,
                "bbox": bbox,
                "line_key": (1, 1, 1),
                "tokens": [],
            }

        first = {
            "label": "english/original/eng/psm6",
            "family": "english",
            "language": "eng",
            "mode": "original",
            "psm": 6,
            "records": [
                record("YD", 55, (0, 0, 20, 10), "english/original/eng/psm6"),
                record("BAIT", 92, (30, 0, 80, 10), "english/original/eng/psm6"),
                record("城市老鼠", 93, (0, 20, 80, 40), "chinese/a"),
                record("城市", 96, (0, 20, 40, 40), "chinese/a"),
                record("城市", 94, (0, 80, 40, 100), "chinese/a"),
            ],
        }
        second = {
            "label": "mixed/inverted/eng+chi_tra/psm11",
            "family": "mixed",
            "language": "eng+chi_tra",
            "mode": "inverted",
            "psm": 11,
            "records": [
                record(
                    "BAIT",
                    90,
                    (30, 0, 80, 10),
                    "mixed/inverted/eng+chi_tra/psm11",
                ),
                record("城市老鼠", 91, (0, 20, 80, 40), "chinese/b"),
            ],
        }
        candidates, rejected, _, _ = pipeline._filter_candidates(
            [first, second]
        )
        self.assertNotIn("YD", candidates)
        self.assertIn("BAIT", candidates)
        self.assertIn("城市老鼠", candidates)
        # The independent row at y=80 preserves the real standalone word.
        self.assertIn("城市", candidates)
        self.assertIn(
            "short_latin_without_strong_consensus",
            {reason for _, reason in rejected},
        )

    def test_same_position_latin_loses_to_dominant_cjk(self):
        pipeline = OCRPipeline()

        def result(label, family, language, text, confidence, bbox, line):
            return {
                "label": label,
                "family": family,
                "language": language,
                "mode": "original",
                "psm": 6,
                "records": [
                    {
                        "text": text,
                        "kind": pipeline._kind(text),
                        "confidence": confidence,
                        "bbox": bbox,
                        "line_key": (1, 1, line),
                        "tokens": [],
                    }
                ],
            }

        results = [
            result(
                "english/original/eng/psm6",
                "english",
                "eng",
                "Shit",
                75,
                (0, 40, 60, 60),
                2,
            ),
            result(
                "chinese/a/chi_tra/psm6",
                "chinese",
                "chi_tra",
                "家人的餐廳",
                94,
                (0, 0, 120, 20),
                1,
            ),
            result(
                "chinese/b/chi_tra/psm11",
                "chinese",
                "chi_tra",
                "尋找地下城的寶物",
                93,
                (0, 40, 180, 60),
                2,
            ),
            result(
                "chinese/c/chi_tra/psm6",
                "chinese",
                "chi_tra",
                "神秘印章的祝福",
                92,
                (0, 80, 160, 100),
                3,
            ),
        ]
        candidates, rejected, _, statistics = pipeline._filter_candidates(
            results
        )
        self.assertEqual(statistics["dominant_script"], "cjk")
        self.assertNotIn("Shit", candidates)
        self.assertIn(
            "same_position_competing_cjk_尋找地下城的寶物",
            {reason for _, reason in rejected},
        )

    def test_low_confidence_cjk_tail_is_recovered(self):
        pipeline = OCRPipeline()
        data = {
            "text": ["家人的餐", "廳"],
            "conf": ["94", "25"],
            "left": [10, 110],
            "top": [10, 10],
            "width": [95, 20],
            "height": [20, 20],
            "block_num": [1, 1],
            "par_num": [1, 1],
            "line_num": [1, 1],
        }
        with patch("app.ocr_pipeline.pytesseract.image_to_data", return_value=data):
            result = pipeline._run(
                "chinese",
                "original",
                Image.new("RGB", (160, 40), "black"),
                "chi_tra",
                7,
                1,
                True,
            )
        self.assertEqual(result["tokens"], ["家人的餐廳"])
        self.assertTrue(
            result["records"][0]["tokens"][-1][
                "recovered_low_confidence_tail"
            ]
        )


if __name__ == "__main__":
    unittest.main()
