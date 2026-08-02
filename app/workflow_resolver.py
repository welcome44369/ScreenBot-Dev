"""Resolve resource references into the legacy WorkflowRunner schema."""
from copy import deepcopy

from app.trigger_conditions import normalize_trigger_payload


class WorkflowResolver:
    def __init__(self, trigger_store, macro_store):
        self.trigger_store = trigger_store
        self.macro_store = macro_store

    def resolve(self, workflow):
        if not isinstance(workflow, dict):
            raise ValueError("Workflow must be an object")
        resolved = deepcopy(workflow)
        steps = resolved.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("Workflow must contain at least one step")
        for step in steps:
            if step.get("trigger_ref"):
                trigger = self.trigger_store.load_trigger(step["trigger_ref"])
                resolved_trigger = {
                    key: trigger[key]
                    for key in (
                        "type",
                        "event",
                        "condition",
                        "text",
                        "texts",
                        "region",
                        "poll_interval_ms",
                        "confirm_frames",
                        "cooldown_ms",
                        "min_absent_duration_ms",
                        "observation",
                    )
                    if key in trigger
                }
                step["trigger"] = normalize_trigger_payload(resolved_trigger)
            elif not isinstance(step.get("trigger"), dict):
                raise ValueError(f"Step {step.get('id', '(unknown)')} is missing trigger or trigger_ref")
            elif step["trigger"].get("type", "text") == "text":
                step["trigger"] = normalize_trigger_payload(
                    step["trigger"], legacy_event_authoritative=True
                )
            if step.get("macro_ref"):
                macro_id = step["macro_ref"]
                filename = macro_id if str(macro_id).endswith(".json") else f"{macro_id}.json"
                self.macro_store.load_script(filename)
                step["macro"] = filename
            elif not isinstance(step.get("macro"), str) or not step["macro"].strip():
                raise ValueError(f"Step {step.get('id', '(unknown)')} is missing macro or macro_ref")

        loop = resolved.get("loop")
        if isinstance(loop, dict) and loop.get("stop_trigger_ref"):
            trigger = self.trigger_store.load_trigger(loop["stop_trigger_ref"])
            loop["stop_trigger"] = normalize_trigger_payload({
                key: trigger[key]
                for key in (
                    "id",
                    "name",
                    "type",
                    "event",
                    "condition",
                    "text",
                    "texts",
                    "region",
                    "poll_interval_ms",
                    "confirm_frames",
                    "cooldown_ms",
                    "min_absent_duration_ms",
                    "observation",
                )
                if key in trigger
            })
        elif (
            isinstance(loop, dict)
            and isinstance(loop.get("stop_trigger"), dict)
            and loop["stop_trigger"].get("type", "text") == "text"
        ):
            loop["stop_trigger"] = normalize_trigger_payload(
                loop["stop_trigger"], legacy_event_authoritative=True
            )
        return resolved
