"""Shared confidence-aware OCR for trigger creation and runtime polling."""
from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import logging
import re
import time

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat
import pytesseract


_CJK_PATTERN = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


class OCRPipeline:
    """Recognize complete CJK lines while rejecting unsupported OCR noise."""

    def __init__(self, logger=None, minimum_confidence=35, process_diagnostics=None):
        self.logger = logger or logging.getLogger("ScreenBot.OCRPipeline")
        self.minimum_confidence = minimum_confidence
        self._profile_cache = {}
        self._process_diagnostics = process_diagnostics

    def set_process_diagnostics(self, diagnostics):
        """Attach the gated observer without changing OCR behavior."""
        self._process_diagnostics = diagnostics

    def _diagnostic_variant_started(self, variant_id, **metadata):
        diagnostics = self._process_diagnostics
        if diagnostics is None or not getattr(diagnostics, "active", False):
            return None
        try:
            return diagnostics.variant_started(variant_id, **metadata)
        except TimeoutError:
            raise
        except Exception:
            self.logger.exception("OCR process diagnostic variant start failed")
            return None

    def _diagnostic_variant_finished(self, token, error=None):
        diagnostics = self._process_diagnostics
        if diagnostics is None or token is None:
            return
        try:
            diagnostics.variant_finished(token, error=error)
        except Exception:
            self.logger.exception("OCR process diagnostic variant finish failed")

    @staticmethod
    def _normalize(value):
        return re.sub(r"\s+", "", value or "").casefold()

    @classmethod
    def _unique(cls, values):
        seen = set()
        result = []
        for value in values:
            value = (value or "").strip()
            key = cls._normalize(value)
            if key and key not in seen:
                seen.add(key)
                result.append(value)
        return result

    @staticmethod
    def _character_counts(value):
        value = value or ""
        cjk = len(re.findall(f"[{_CJK_PATTERN}]", value))
        latin = len(re.findall(r"[A-Za-z]", value))
        digits = len(re.findall(r"\d", value))
        symbols = len(
            re.findall(
                f"[^A-Za-z0-9\\s{_CJK_PATTERN}]",
                value,
            )
        )
        return cjk, latin, digits, symbols

    @classmethod
    def _kind(cls, value):
        value = (value or "").strip()
        cjk, latin, digits, symbols = cls._character_counts(value)
        if not value or symbols or (cjk and (latin or digits)):
            return "noise"
        if cjk and not latin and not digits:
            return "cjk"
        if (
            latin
            and not cjk
            and not digits
            and re.fullmatch(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", value)
        ):
            return "latin"
        if digits and not cjk and not latin:
            return "numeric"
        return "noise"

    @classmethod
    def _clean_detected_text(cls, value):
        """Remove OCR punctuation while preserving real CJK or Latin words."""
        value = re.sub(r"\s+", " ", (value or "").strip())
        cjk, latin, digits, _ = cls._character_counts(value)
        if cjk and not latin:
            # Trigger candidates are textual; brackets, bullets and counters
            # around a CJK line are not useful for exact substring matching.
            return re.sub(f"[^{_CJK_PATTERN}]", "", value)
        if latin and not cjk and not digits:
            return value.strip("`~!@#$%^&*()_=+[]{}\\|;:\",.<>/?“”‘’")
        return value

    @staticmethod
    def _white_text_statistics(image):
        gray = ImageOps.grayscale(image)
        histogram = gray.histogram()
        pixels = max(1, gray.width * gray.height)
        bright_ratio = sum(histogram[210:]) / pixels
        dark_ratio = sum(histogram[:110]) / pixels
        edge_image = gray.filter(ImageFilter.FIND_EDGES)
        edge_mean = ImageStat.Stat(edge_image).mean[0]
        contrast = ImageStat.Stat(gray).stddev[0]
        detected = (
            dark_ratio >= 0.22
            and 0.004 <= bright_ratio <= 0.48
            and contrast >= 28
            and edge_mean >= 8
        )
        return {
            "detected": detected,
            "bright_ratio": bright_ratio,
            "dark_ratio": dark_ratio,
            "contrast": contrast,
            "edge_mean": edge_mean,
        }

    @staticmethod
    def _resize(gray, scale):
        return gray.resize(
            (max(1, gray.width * scale), max(1, gray.height * scale)),
            Image.Resampling.LANCZOS,
        )

    @classmethod
    def _preprocess_images(cls, image, white_text_detected):
        gray = ImageOps.grayscale(image)
        upscaled = ImageOps.autocontrast(cls._resize(gray, 3)).filter(
            ImageFilter.SHARPEN
        )
        images = {
            "original": (image, 1),
            "upscaled": (upscaled, 3),
        }
        if not white_text_detected:
            return images

        inverted = ImageOps.autocontrast(
            cls._resize(ImageOps.invert(gray), 3)
        ).filter(ImageFilter.SHARPEN)
        white_autocontrast = ImageOps.autocontrast(
            cls._resize(gray, 3), cutoff=1
        ).filter(ImageFilter.SHARPEN)

        local_source = cls._resize(gray, 3)
        local_background = local_source.filter(ImageFilter.GaussianBlur(15))
        local_difference = ImageChops.subtract(
            local_source, local_background, scale=1.0, offset=128
        )
        adaptive = local_difference.point(
            lambda value: 0 if value >= 142 else 255,
            mode="1",
        ).convert("L")
        images.update(
            {
                "white_text_inverted": (inverted, 3),
                "white_text_autocontrast": (white_autocontrast, 3),
                "white_text_adaptive_threshold": (adaptive, 3),
            }
        )
        return images

    @staticmethod
    def _bbox(entries, scale):
        left = min(entry["left"] for entry in entries) / scale
        top = min(entry["top"] for entry in entries) / scale
        right = max(entry["left"] + entry["width"] for entry in entries) / scale
        bottom = max(entry["top"] + entry["height"] for entry in entries) / scale
        return (left, top, right, bottom)

    def _record(self, entries, scale):
        text = "".join(entry["clean_text"] for entry in entries)
        confidence = sum(entry["confidence"] for entry in entries) / len(entries)
        return {
            "text": text,
            "kind": self._kind(text),
            "confidence": confidence,
            "bbox": self._bbox(entries, scale),
            "line_key": entries[0]["line_key"],
            "tokens": [
                {
                    "text": entry["clean_text"],
                    "raw_text": entry["raw_text"],
                    "confidence": entry["confidence"],
                    "bbox": (
                        entry["left"] / scale,
                        entry["top"] / scale,
                        (entry["left"] + entry["width"]) / scale,
                        (entry["top"] + entry["height"]) / scale,
                    ),
                    "recovered_low_confidence_tail": entry.get(
                        "recovered_low_confidence_tail", False
                    ),
                }
                for entry in entries
            ],
        }

    def _run(
        self,
        family,
        mode,
        image,
        language,
        psm,
        scale,
        white_text_detected,
    ):
        label = f"{family}/{mode}/{language}/psm{psm}"
        self.logger.info("OCR_PIPELINE_START %s", label)
        started = time.perf_counter()
        diagnostic_token = self._diagnostic_variant_started(
            label,
            call="pytesseract.image_to_data",
            family=family,
            mode=mode,
            language=language,
            psm=psm,
            scale=scale,
        )
        try:
            data = pytesseract.image_to_data(
                image,
                lang=language,
                config=f"--psm {psm}",
                output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:
            self._diagnostic_variant_finished(
                diagnostic_token,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        else:
            self._diagnostic_variant_finished(diagnostic_token)
        duration = int((time.perf_counter() - started) * 1000)
        entries = []
        raw_tokens = []
        rejected = []
        count = len(data.get("text", []))
        for index in range(count):
            raw_text = (data["text"][index] or "").strip()
            if not raw_text:
                continue
            raw_tokens.append(raw_text)
            try:
                confidence = float(data.get("conf", ["-1"] * count)[index])
            except (TypeError, ValueError):
                confidence = -1.0
            clean_text = self._clean_detected_text(raw_text)
            kind = self._kind(clean_text)
            entry = {
                "raw_text": raw_text,
                "clean_text": clean_text,
                "confidence": confidence,
                "left": int(data.get("left", [0] * count)[index]),
                "top": int(data.get("top", [0] * count)[index]),
                "width": int(data.get("width", [0] * count)[index]),
                "height": int(data.get("height", [0] * count)[index]),
                "line_key": (
                    int(data.get("block_num", [0] * count)[index]),
                    int(data.get("par_num", [0] * count)[index]),
                    int(data.get("line_num", [0] * count)[index]),
                ),
                "kind": kind,
            }
            if kind == "noise":
                rejected.append((raw_text, "noise_characters"))
            elif entry["width"] * entry["height"] < 6:
                rejected.append((raw_text, "bounding_box_too_small"))
            else:
                entry["below_minimum"] = confidence < self.minimum_confidence
                entries.append(entry)

        grouped = defaultdict(list)
        for entry in entries:
            grouped[entry["line_key"]].append(entry)

        records = []
        for line_entries in grouped.values():
            line_entries.sort(key=lambda entry: entry["left"])
            high_cjk = [
                entry
                for entry in line_entries
                if entry["kind"] == "cjk" and not entry["below_minimum"]
            ]
            rightmost_high = max(
                high_cjk,
                key=lambda entry: entry["left"] + entry["width"],
                default=None,
            )
            median_height = (
                sorted(entry["height"] for entry in high_cjk)[len(high_cjk) // 2]
                if high_cjk
                else 0
            )
            high_cjk_characters = sum(
                self._character_counts(entry["clean_text"])[0]
                for entry in high_cjk
            )
            for entry in line_entries:
                if not entry["below_minimum"]:
                    continue
                recover_tail = False
                if (
                    entry["kind"] == "cjk"
                    and entry["confidence"] >= 15
                    and rightmost_high is not None
                    and high_cjk_characters >= 2
                ):
                    previous_right = (
                        rightmost_high["left"] + rightmost_high["width"]
                    )
                    gap = entry["left"] - previous_right
                    height_ratio = entry["height"] / max(1, median_height)
                    recover_tail = (
                        entry["left"] >= rightmost_high["left"]
                        and gap <= max(12 * scale, median_height * 1.25)
                        and 0.55 <= height_ratio <= 1.8
                        and entry["left"] + entry["width"] <= image.width
                    )
                if recover_tail:
                    entry["below_minimum"] = False
                    entry["recovered_low_confidence_tail"] = True
                    self.logger.info(
                        "OCR_CJK_TAIL_RECOVERED text=%r confidence=%.1f "
                        "bbox=%s line_id=%s",
                        entry["clean_text"],
                        entry["confidence"],
                        (
                            entry["left"] / scale,
                            entry["top"] / scale,
                            (entry["left"] + entry["width"]) / scale,
                            (entry["top"] + entry["height"]) / scale,
                        ),
                        entry["line_key"],
                    )
                else:
                    rejected.append(
                        (entry["raw_text"], "confidence_below_minimum")
                    )
            line_entries = [
                entry for entry in line_entries if not entry["below_minimum"]
            ]
            pending_cjk = []
            last_right = None
            last_height = 0

            def flush_cjk():
                if pending_cjk:
                    records.append(self._record(pending_cjk, scale))
                    pending_cjk.clear()

            for entry in line_entries:
                if entry["kind"] == "cjk":
                    gap = entry["left"] - (
                        last_right if last_right is not None else entry["left"]
                    )
                    allowed = max(
                        12 * scale,
                        int(max(last_height, entry["height"]) * 1.75),
                    )
                    if pending_cjk and gap > allowed:
                        flush_cjk()
                    pending_cjk.append(entry)
                    last_right = entry["left"] + entry["width"]
                    last_height = entry["height"]
                else:
                    flush_cjk()
                    if entry["kind"] == "latin":
                        records.append(self._record([entry], scale))
                    last_right = None
                    last_height = 0
            flush_cjk()

        target_kind = {"english": "latin", "chinese": "cjk"}.get(family)
        if target_kind:
            records = [
                record for record in records if record["kind"] == target_kind
            ]

        for record in records:
            if record["kind"] != "cjk":
                continue
            last_token_right = max(
                token["bbox"][2] for token in record["tokens"]
            )
            self.logger.info(
                "OCR_LINE_RIGHT_EDGE text=%r edge=%.1f "
                "OCR_LAST_TOKEN_RIGHT %.1f OCR_RIGHT_MARGIN %.1f",
                record["text"],
                record["bbox"][2],
                last_token_right,
                max(0.0, image.width / scale - last_token_right),
            )

        valid_characters = sum(
            sum(self._character_counts(record["text"])[:3])
            for record in records
        )
        cjk_characters = sum(
            self._character_counts(record["text"])[0] for record in records
        )
        latin_characters = sum(
            self._character_counts(record["text"])[1] for record in records
        )
        average_confidence = (
            sum(record["confidence"] for record in records) / len(records)
            if records
            else 0.0
        )
        noise_ratio = len(rejected) / max(1, len(raw_tokens))
        cjk_ratio = cjk_characters / max(1, valid_characters)
        latin_ratio = latin_characters / max(1, valid_characters)
        completeness = max(
            (
                min(12, len(self._normalize(record["text"])))
                for record in records
            ),
            default=0,
        )
        white_bonus = (
            7
            if white_text_detected and mode.startswith("white_text_")
            else 0
        )
        noise_score = noise_ratio * 100
        score = (
            average_confidence
            + completeness * 1.5
            + white_bonus
            - noise_score * 0.35
        )
        result = {
            "label": label,
            "family": family,
            "language": language,
            "mode": mode,
            "psm": psm,
            "scale": scale,
            "tokens": self._unique(record["text"] for record in records),
            "records": records,
            "raw_tokens": raw_tokens,
            "average_confidence": average_confidence,
            "noise_ratio": noise_ratio,
            "noise_score": noise_score,
            "cjk_ratio": cjk_ratio,
            "latin_ratio": latin_ratio,
            "score": score,
            "rejected": rejected,
            "duration_ms": duration,
        }
        self.logger.info(
            "OCR_PIPELINE_MODE %s OCR_LANGUAGE %s OCR_PSM %s",
            mode,
            language,
            psm,
        )
        self.logger.info(
            "OCR_PREPROCESS_MODE %s OCR_SCALE_FACTOR %s OCR_DURATION_MS %s",
            mode,
            scale,
            duration,
        )
        self.logger.info("OCR_PIPELINE_RAW %r", raw_tokens)
        self.logger.info("OCR_PIPELINE_TOKENS %s", result["tokens"])
        self.logger.info(
            "OCR_PIPELINE_CONFIDENCE %.1f OCR_CJK_RATIO %.3f "
            "OCR_LATIN_RATIO %.3f OCR_NOISE_SCORE %.1f "
            "OCR_FINAL_SCORE %.1f",
            average_confidence,
            cjk_ratio,
            latin_ratio,
            noise_score,
            score,
        )
        return result

    @staticmethod
    def _overlap_same_line(first, second):
        left_a, top_a, right_a, bottom_a = first["bbox"]
        left_b, top_b, right_b, bottom_b = second["bbox"]
        vertical_overlap = max(0.0, min(bottom_a, bottom_b) - max(top_a, top_b))
        min_height = max(1.0, min(bottom_a - top_a, bottom_b - top_b))
        if vertical_overlap / min_height < 0.55:
            return False
        horizontal_overlap = max(
            0.0, min(right_a, right_b) - max(left_a, left_b)
        )
        min_width = max(1.0, min(right_a - left_a, right_b - left_b))
        center_a = (left_a + right_a) / 2
        center_b = (left_b + right_b) / 2
        return (
            horizontal_overlap / min_width >= 0.45
            or abs(center_a - center_b) <= max(min_width, 20)
        )

    def _aggregate(self, results):
        groups = defaultdict(list)
        for result in results:
            for record in result["records"]:
                enriched = dict(record)
                enriched["pipeline"] = result["label"]
                enriched["language"] = result["language"]
                enriched["mode"] = result["mode"]
                enriched["psm"] = result["psm"]
                enriched["family"] = result["family"]
                enriched["line_id"] = ":".join(
                    str(value) for value in record["line_key"]
                )
                groups[self._normalize(record["text"])].append(enriched)
        return groups

    def _filter_candidates(self, results):
        groups = self._aggregate(results)
        accepted = {}
        rejected = []
        all_records = [
            record for records in groups.values() for record in records
        ]
        cjk_characters = sum(
            self._character_counts(record["text"])[0]
            for record in all_records
        )
        latin_characters = sum(
            self._character_counts(record["text"])[1]
            for record in all_records
        )
        script_characters = max(1, cjk_characters + latin_characters)
        crop_cjk_ratio = cjk_characters / script_characters
        crop_latin_ratio = latin_characters / script_characters
        cjk_rows = []
        latin_rows = []
        for record in sorted(
            all_records,
            key=lambda item: (item["bbox"][1], item["bbox"][0]),
        ):
            if record["confidence"] < self.minimum_confidence:
                continue
            center = (record["bbox"][1] + record["bbox"][3]) / 2
            height = max(1.0, record["bbox"][3] - record["bbox"][1])
            rows = cjk_rows if record["kind"] == "cjk" else latin_rows
            if record["kind"] not in {"cjk", "latin"}:
                continue
            if not any(
                abs(center - existing_center)
                <= max(height, existing_height) * 0.55
                for existing_center, existing_height in rows
            ):
                rows.append((center, height))
        cjk_dominant = crop_cjk_ratio >= 0.65 and len(cjk_rows) >= 2
        script_statistics = {
            "dominant_script": "cjk" if cjk_dominant else (
                "latin" if crop_latin_ratio >= 0.65 else "mixed"
            ),
            "cjk_ratio": crop_cjk_ratio,
            "latin_ratio": crop_latin_ratio,
            "valid_cjk_lines": len(cjk_rows),
            "valid_latin_lines": len(latin_rows),
        }
        self.logger.info(
            "OCR_SCRIPT_DOMINANCE dominant=%s cjk_ratio=%.3f "
            "latin_ratio=%.3f cjk_lines=%s latin_lines=%s",
            script_statistics["dominant_script"],
            crop_cjk_ratio,
            crop_latin_ratio,
            len(cjk_rows),
            len(latin_rows),
        )
        for key, records in groups.items():
            display = max(records, key=lambda record: record["confidence"])["text"]
            kind = self._kind(display)
            support = len({record["pipeline"] for record in records})
            average = sum(record["confidence"] for record in records) / len(records)
            maximum = max(record["confidence"] for record in records)
            reason = None
            if kind == "latin":
                length = len(re.sub(r"[^A-Za-z]", "", display))
                if length <= 3 and not (support >= 3 and average >= 82):
                    reason = "short_latin_without_strong_consensus"
                elif length > 3 and support < 2 and maximum < 72:
                    reason = "latin_without_confidence_or_consensus"
            elif kind == "cjk":
                length = len(self._normalize(display))
                if length <= 1 and support < 3:
                    reason = "isolated_cjk_without_consensus"
                elif support < 2 and average < 78:
                    reason = "cjk_without_confidence_or_consensus"
            elif kind != "cjk":
                reason = "unsupported_candidate_kind"
            if reason:
                rejected.append((display, reason))
                continue
            accepted[key] = {
                "text": display,
                "kind": kind,
                "records": records,
                "support": support,
                "confidence": average,
                "top": min(record["bbox"][1] for record in records),
                "left": min(record["bbox"][0] for record in records),
                "bbox": max(
                    records,
                    key=lambda record: record["confidence"],
                )["bbox"],
            }

        def quality(candidate):
            return (
                min(candidate["support"], 2) * 7
                + candidate["confidence"]
                + min(12, len(self._normalize(candidate["text"]))) * 2.5
            )

        # Cluster alternate readings at the same line/location. Consensus and
        # confidence choose the canonical reading; length alone never wins.
        selected_cjk = []
        cjk_candidates = sorted(
            (
                candidate
                for candidate in accepted.values()
                if candidate["kind"] == "cjk"
            ),
            key=quality,
            reverse=True,
        )
        for candidate in cjk_candidates:
            candidate_key = self._normalize(candidate["text"])
            replaced_by = None
            for chosen in selected_cjk:
                chosen_key = self._normalize(chosen["text"])
                related = (
                    candidate_key in chosen_key
                    or chosen_key in candidate_key
                    or SequenceMatcher(
                        None, candidate_key, chosen_key
                    ).ratio()
                    >= 0.60
                )
                if not related:
                    continue
                every_occurrence_overlaps = all(
                    any(
                        self._overlap_same_line(candidate_record, chosen_record)
                        for chosen_record in chosen["records"]
                    )
                    for candidate_record in candidate["records"]
                )
                if every_occurrence_overlaps:
                    replaced_by = chosen
                    break
            if replaced_by is not None:
                rejected.append(
                    (
                        candidate["text"],
                        "same_line_lower_quality_variant_of_"
                        f"{replaced_by['text']}",
                    )
                )
            else:
                selected_cjk.append(candidate)

        # A Latin and CJK reading over the same pixels are competing OCR
        # interpretations, not two independent lines. Prefer the established
        # CJK line in a CJK-dominant crop; real bilingual rows at distinct
        # positions remain independent.
        for key, candidate in list(accepted.items()):
            if candidate["kind"] != "latin":
                continue
            competing_cjk = next(
                (
                    cjk_candidate
                    for cjk_candidate in selected_cjk
                    if any(
                        self._overlap_same_line(latin_record, cjk_record)
                        for latin_record in candidate["records"]
                        for cjk_record in cjk_candidate["records"]
                    )
                ),
                None,
            )
            if competing_cjk is not None and (
                cjk_dominant or quality(competing_cjk) >= quality(candidate)
            ):
                rejected.append(
                    (
                        candidate["text"],
                        "same_position_competing_cjk_"
                        f"{competing_cjk['text']}",
                    )
                )
                accepted.pop(key, None)

        for key, candidate in list(accepted.items()):
            if candidate["kind"] != "latin" or not cjk_dominant:
                continue
            reason = None
            if candidate["support"] < 2:
                reason = "isolated_latin_in_cjk_dominant_region"
            elif candidate["confidence"] < 75:
                reason = "weak_latin_in_cjk_dominant_region"
            if reason:
                rejected.append((candidate["text"], reason))
                accepted.pop(key, None)

        selected_cjk_ids = {id(candidate) for candidate in selected_cjk}
        for key, candidate in list(accepted.items()):
            if (
                candidate["kind"] == "cjk"
                and id(candidate) not in selected_cjk_ids
            ):
                accepted.pop(key, None)

        ordered = sorted(
            accepted.values(),
            key=lambda candidate: (
                candidate["top"],
                candidate["left"],
                -len(candidate["text"]),
            ),
        )
        return (
            [candidate["text"] for candidate in ordered],
            rejected,
            ordered,
            script_statistics,
        )

    @staticmethod
    def _best(results, family):
        available = [
            result
            for result in results
            if result["family"] == family and result["tokens"]
        ]
        return max(available, key=lambda result: result["score"]) if available else None

    @staticmethod
    def _candidate_metadata(candidate):
        best = max(
            candidate["records"],
            key=lambda record: record["confidence"],
        )
        sources = [
            {
                "language": record["language"],
                "pipeline": record["pipeline"],
                "mode": record["mode"],
                "psm": record["psm"],
                "confidence": record["confidence"],
                "bounding_box": record["bbox"],
                "line_id": record["line_id"],
                "tokens": record.get("tokens", []),
            }
            for record in candidate["records"]
        ]
        return {
            "text": candidate["text"],
            "language": best["language"],
            "pipeline": best["pipeline"],
            "psm": best["psm"],
            "confidence": candidate["confidence"],
            "bounding_box": best["bbox"],
            "line_id": best["line_id"],
            "support_count": candidate["support"],
            "sources": sources,
        }

    def _log_candidate(self, metadata, accepted, reason=None):
        self.logger.info(
            "OCR_CANDIDATE_SUPPORT text=%r support_count=%s",
            metadata["text"],
            metadata["support_count"],
        )
        self.logger.info(
            "OCR_CANDIDATE_BBOX text=%r bbox=%s line_id=%s",
            metadata["text"],
            metadata["bounding_box"],
            metadata["line_id"],
        )
        for source in metadata["sources"]:
            self.logger.info(
                "OCR_CANDIDATE_SOURCE text=%r language=%s pipeline=%s "
                "psm=%s confidence=%.1f bounding_box=%s line_id=%s",
                metadata["text"],
                source["language"],
                source["pipeline"],
                source["psm"],
                source["confidence"],
                source["bounding_box"],
                source["line_id"],
            )
        if accepted:
            self.logger.info(
                "OCR_CANDIDATE_ACCEPTED text=%r",
                metadata["text"],
            )
        else:
            self.logger.info(
                "OCR_CANDIDATE_REJECTED text=%r "
                "OCR_REJECTION_REASON %s",
                metadata["text"],
                reason,
            )

    def recognize_text(self, image, languages="eng+chi_tra", layout_hint="multi_line"):
        language_token = self._diagnostic_variant_started(
            "get_languages",
            call="pytesseract.get_languages",
            config="",
        )
        try:
            installed = set(pytesseract.get_languages(config=""))
        except Exception as exc:
            self._diagnostic_variant_finished(
                language_token,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        else:
            self._diagnostic_variant_finished(language_token)
        if "eng" not in installed:
            raise RuntimeError(
                "Tesseract English language data (eng) is not installed."
            )
        language_set = set((languages or "eng").split("+"))
        self.logger.info("OCR_CROP_SIZE width=%s height=%s", image.width, image.height)
        white_stats = self._white_text_statistics(image)
        self.logger.info(
            "OCR_WHITE_TEXT_DETECTED %s bright_ratio=%.4f "
            "dark_ratio=%.4f contrast=%.2f edge_mean=%.2f",
            str(white_stats["detected"]).lower(),
            white_stats["bright_ratio"],
            white_stats["dark_ratio"],
            white_stats["contrast"],
            white_stats["edge_mean"],
        )
        processed = self._preprocess_images(image, white_stats["detected"])
        base_psm = 7 if layout_hint == "single_line" else 6
        full_specs = [
            ("english", "original", "eng", base_psm),
            ("english", "upscaled", "eng", 11),
        ]
        if "chi_tra" in installed and "chi_tra" in language_set:
            full_specs.extend(
                [
                    ("chinese", "original", "chi_tra", 11),
                    ("chinese", "upscaled", "chi_tra", base_psm),
                    ("mixed", "original", "eng+chi_tra", 11),
                ]
            )
            if white_stats["detected"]:
                for mode in (
                    "white_text_inverted",
                    "white_text_autocontrast",
                    "white_text_adaptive_threshold",
                ):
                    full_specs.extend(
                        [
                            ("chinese", mode, "chi_tra", 6),
                            ("chinese", mode, "chi_tra", 7),
                            ("chinese", mode, "chi_tra", 11),
                            ("mixed", mode, "eng+chi_tra", 6),
                            ("mixed", mode, "eng+chi_tra", 11),
                        ]
                    )
        else:
            self.logger.warning("OCR_CHINESE_PIPELINE_SKIPPED missing=chi_tra")

        profile_key = (
            bool(white_stats["detected"]),
            layout_hint,
            image.width,
            image.height,
            round(white_stats["bright_ratio"], 2),
            round(white_stats["dark_ratio"], 1),
            languages,
        )
        specs = self._profile_cache.get(profile_key, full_specs)
        if profile_key in self._profile_cache:
            self.logger.info("OCR_PIPELINE_PROFILE_CACHE_HIT specs=%s", specs)
        results = []
        for family, mode, language, psm in specs:
            processed_image, scale = processed[mode]
            results.append(
                self._run(
                    family,
                    mode,
                    processed_image,
                    language,
                    psm,
                    scale,
                    white_stats["detected"],
                )
            )

        # Reward repeatable text across independent preprocess/PSM passes.
        support = defaultdict(set)
        for result in results:
            for token in result["tokens"]:
                support[self._normalize(token)].add(result["label"])
        for result in results:
            if result["tokens"]:
                consensus = sum(
                    min(4, len(support[self._normalize(token)]))
                    for token in result["tokens"]
                ) / len(result["tokens"])
                result["consensus"] = consensus
                result["score"] += consensus * 2.5
            else:
                result["consensus"] = 0.0

        english = self._best(results, "english")
        chinese = self._best(results, "chinese")
        mixed = self._best(results, "mixed")
        if english:
            self.logger.info(
                "OCR_SELECTED_ENGLISH_PIPELINE %s", english["label"]
            )
        if chinese:
            self.logger.info(
                "OCR_SELECTED_CHINESE_PIPELINE %s", chinese["label"]
            )
        if profile_key not in self._profile_cache:
            selected_specs = []
            for family, limit in (("english", 1), ("chinese", 2), ("mixed", 1)):
                family_results = sorted(
                    (
                        result
                        for result in results
                        if result["family"] == family and result["tokens"]
                    ),
                    key=lambda result: result["score"],
                    reverse=True,
                )
                for result in family_results[:limit]:
                    spec = (
                        result["family"],
                        result["mode"],
                        result["language"],
                        result["psm"],
                    )
                    if spec not in selected_specs:
                        selected_specs.append(spec)
            if selected_specs:
                self._profile_cache[profile_key] = selected_specs
                self.logger.info(
                    "OCR_PIPELINE_PROFILE_CACHE_STORE specs=%s",
                    selected_specs,
                )

        (
            candidates,
            rejected,
            selected_candidates,
            script_statistics,
        ) = self._filter_candidates(results)
        grouped_sources = self._aggregate(results)
        accepted_metadata = [
            self._candidate_metadata(candidate)
            for candidate in selected_candidates
        ]
        for metadata in accepted_metadata:
            self._log_candidate(metadata, True)

        rejected_metadata = []
        for text, reason in rejected:
            records = grouped_sources.get(self._normalize(text), [])
            detail = {
                "text": text,
                "reason": reason,
                "support_count": len(
                    {record["pipeline"] for record in records}
                ),
                "sources": [
                    {
                        "language": record["language"],
                        "pipeline": record["pipeline"],
                        "mode": record["mode"],
                        "psm": record["psm"],
                        "confidence": record["confidence"],
                        "bounding_box": record["bbox"],
                        "line_id": record["line_id"],
                        "tokens": record.get("tokens", []),
                    }
                    for record in records
                ],
            }
            rejected_metadata.append(detail)
            if records:
                candidate = {
                    "text": text,
                    "records": records,
                    "support": detail["support_count"],
                    "confidence": sum(
                        record["confidence"] for record in records
                    ) / len(records),
                }
                self._log_candidate(
                    self._candidate_metadata(candidate),
                    False,
                    reason,
                )
            self.logger.info(
                "OCR_REJECTED_CANDIDATES %r OCR_REJECTION_REASON %s",
                text,
                reason,
            )
        confidence = max(
            (
                candidate["confidence"]
                for candidate in selected_candidates
            ),
            default=0.0,
        )
        boundary_warnings = []
        for metadata in accepted_metadata:
            right_edge = metadata["bounding_box"][2]
            right_margin = max(0.0, image.width - right_edge)
            if right_margin <= max(4.0, image.width * 0.02):
                boundary_warnings.append(
                    {
                        "text": metadata["text"],
                        "right_margin": right_margin,
                        "message": (
                            "文字可能接近選區邊界，"
                            "建議稍微擴大框選範圍。"
                        ),
                    }
                )
        clean_text = "\n".join(candidates)
        final = {
            "raw_text": clean_text,
            "clean_text": clean_text,
            "candidates": candidates,
            "confidence": confidence,
            "pipeline_metadata": results,
            "candidate_metadata": accepted_metadata,
            "script_statistics": script_statistics,
            "white_text_detected": white_stats["detected"],
            "white_text_statistics": white_stats,
            "rejected_candidates": rejected_metadata,
            "boundary_warnings": boundary_warnings,
        }
        self.logger.info("OCR_FINAL_TEXT %r", clean_text)
        self.logger.info("OCR_FINAL_CANDIDATES %s", candidates)
        return final
