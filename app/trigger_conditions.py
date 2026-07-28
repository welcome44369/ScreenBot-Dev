"""Shared serialization and presentation metadata for text trigger conditions."""
from __future__ import annotations

CONDITION_CHOICES = (
    ("initial_absent", "初始不存在  0", "開始偵測後，第一個確認結果為「不存在」時觸發一次。若第一個確認結果為存在，本次工作流不觸發。", "initial", "absent"),
    ("initial_present", "初始存在  1", "開始偵測後，第一個確認結果為「存在」時觸發一次。若第一個確認結果為不存在，本次工作流不觸發。", "initial", "present"),
    ("edge_present", "出現  0→1", "目標必須先確認為不存在，之後確認為存在時觸發。", "edge", "present"),
    ("edge_absent", "消失  1→0", "目標必須先確認為存在，之後確認為不存在時觸發。", "edge", "absent"),
    ("state_absent", "目前不存在  0 + 1→0", "開始時已不存在，或之後由存在變成不存在時觸發。同一段不存在狀態只觸發一次。", "state", "absent"),
    ("state_present", "目前存在  1 + 0→1", "開始時已存在，或之後由不存在變成存在時觸發。同一段存在狀態只觸發一次。", "state", "present"),
)

LEGACY_APPEAR = "legacy_appear"
LEGACY_DISAPPEAR = "legacy_disappear"


def condition_from_code(code):
    for item_code, _label, _help, mode, desired_state in CONDITION_CHOICES:
        if code == item_code:
            return {"mode": mode, "desired_state": desired_state}
    raise ValueError(f"Unknown trigger condition code: {code}")


def condition_code(condition):
    if not isinstance(condition, dict):
        return None
    pair = (condition.get("mode"), condition.get("desired_state"))
    for item_code, _label, _help, mode, desired_state in CONDITION_CHOICES:
        if pair == (mode, desired_state):
            return item_code
    return None


def normalize_condition(condition=None, event=None):
    """Return ``(canonical_condition, legacy_mode)`` without mutating input."""
    if condition is not None:
        code = condition_code(condition)
        if code is None:
            raise ValueError("condition.mode must be initial, edge, or state and desired_state must be present or absent")
        return condition_from_code(code), None
    if event == "appear":
        return {"mode": "legacy", "desired_state": "present"}, LEGACY_APPEAR
    if event == "disappear":
        return {"mode": "legacy", "desired_state": "absent"}, LEGACY_DISAPPEAR
    raise ValueError("Text trigger requires a valid condition or legacy event")


def ui_code_from_trigger(trigger):
    trigger = trigger if isinstance(trigger, dict) else {}
    code = condition_code(trigger.get("condition"))
    if code:
        return code
    return {"appear": LEGACY_APPEAR, "disappear": LEGACY_DISAPPEAR}.get(trigger.get("event"))


def label_for_code(code):
    if code == LEGACY_APPEAR:
        return "舊版出現"
    if code == LEGACY_DISAPPEAR:
        return "舊版消失"
    return next((label for item_code, label, _help, _mode, _desired in CONDITION_CHOICES if item_code == code), "未知條件")


def help_for_code(code):
    if code == LEGACY_APPEAR:
        return "開始偵測時若文字已經存在，也會直接觸發。這是舊版相容行為。"
    if code == LEGACY_DISAPPEAR:
        return "保留既有保守的消失確認行為。"
    return next((help_text for item_code, _label, help_text, _mode, _desired in CONDITION_CHOICES if item_code == code), "")


def add_condition_items(combo, include_legacy_code=None):
    combo.clear()
    for code, label, _help, _mode, _desired in CONDITION_CHOICES:
        combo.addItem(label, code)
    if include_legacy_code in {LEGACY_APPEAR, LEGACY_DISAPPEAR}:
        combo.addItem(label_for_code(include_legacy_code), include_legacy_code)


def apply_condition_to_payload(payload, code, *, preserve_legacy=False):
    result = dict(payload)
    if code == LEGACY_APPEAR or (code == LEGACY_DISAPPEAR and preserve_legacy):
        result.pop("condition", None)
        result["event"] = "appear" if code == LEGACY_APPEAR else "disappear"
        return result
    result["condition"] = condition_from_code(code)
    result.pop("event", None)
    return result
