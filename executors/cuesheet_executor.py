from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# -- Thermal conditions --
_THERMAL_CONDITIONS_PATH = (
    Path(__file__).resolve().parents[3] / "playbook" / "lib" / "thermal_conditions.py"
)

_spec = importlib.util.spec_from_file_location("thermal_conditions", _THERMAL_CONDITIONS_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Unable to load thermal conditions module from {_THERMAL_CONDITIONS_PATH}")

_thermal_conditions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_thermal_conditions)

# -- VRGB routing --
_MAESTRO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
_VRGB_LIB = os.path.join(_MAESTRO_ROOT, "playbook", "lib")
if _VRGB_LIB not in sys.path:
    sys.path.insert(0, _VRGB_LIB)

from vrgb_routing import maybe_emit_context_switch

# -- Context budget --
from context_budget import check_budget, create_budget, enforce_budget, register_token

# -- Policy gates --
from policy_gate import PolicyViolation, enforce_gate, evaluate_gate

# -- Trust scope --
from trust_scope import evaluate_trust

# -- Token lifecycle hooks --
import queue
from token_hooks import compile_hooks, execute_hook_action
from token_watcher import watch_tokens

LOGGER = logging.getLogger(__name__)


def build_context_budget(sheet_id, cuesheet_data):
    """Create a budget tracker from cue sheet YAML config.

    Missing context_budget keeps backward compatibility by returning None.
    """
    context_budget = (cuesheet_data or {}).get("context_budget")
    if not context_budget:
        return None

    return create_budget(
        sheet_id=sheet_id,
        max_tokens=context_budget.get("max_tokens", 10),
        max_active=context_budget.get("max_active", 5),
        on_exceed=context_budget.get("on_exceed", "warn_only"),
    )


def execute_cuesheet(
    cuesheet: dict[str, Any],
    tokens_dir: str | Path,
    execute_cue: Callable[[dict[str, Any]], None],
    log: Callable[[str], None],
    emit_token=None,
) -> None:
    """Execute cues in order, skipping cues whose thermal conditions are unmet.
    Emits context-switch signals when hue distance between consecutive cues exceeds threshold."""
    cues = cuesheet.get("cues", [])
    routing = cuesheet.get("routing", {})
    threshold = routing.get("context_switch_threshold", 90)

    prev_cue = None
    for cue in cues:
        when_block = cue.get("when")
        if not _thermal_conditions.evaluate_when(when_block, tokens_dir):
            cue_id = cue.get("id", "<unknown>")
            log(f"[cuesheet] skipped cue '{cue_id}': thermal condition not met")
            continue

        if prev_cue is not None:
            maybe_emit_context_switch(
                prev_cue, cue,
                threshold=threshold,
                emit_token=emit_token,
                logger=log,
            )

        execute_cue(cue)
        prev_cue = cue


def validate_cuesheet_schema(cuesheet: dict[str, Any]) -> None:
    """Validate cue schema including optional thermal-aware when blocks."""
    cues = cuesheet.get("cues", [])
    if not isinstance(cues, list):
        raise ValueError("cues must be a list")

    for cue in cues:
        if not isinstance(cue, dict):
            raise ValueError("Each cue must be a mapping")
        _thermal_conditions.validate_cue_when_block(cue.get("when"))


class CueSheetExecutor:
    """Full executor with context-switch signals and budget enforcement."""

    def __init__(self, sheet_id="default", cuesheet_data=None, tokens_dir=None,
                 token_writer=None, *, context_switch_threshold: float = 90,
                 emit_token=None, logger=None):
        self.sheet_id = sheet_id
        self.cuesheet_data = cuesheet_data or {}
        self.tokens_dir = tokens_dir
        self.token_writer = token_writer
        self.context_switch_threshold = context_switch_threshold
        self.emit_token = emit_token
        self.logger = logger
        self.context_budget = build_context_budget(sheet_id, cuesheet_data)

    def handle_transition(self, current_cue: Any, next_cue: Any) -> Optional[Dict[str, Any]]:
        """Emit informational context-switch token when hue-domain jump exceeds threshold."""
        return maybe_emit_context_switch(
            current_cue,
            next_cue,
            threshold=self.context_switch_threshold,
            emit_token=self.emit_token,
            logger=self.logger,
        )

    def emit_token_with_budget(self, token_payload):
        """Emit one token with context-budget checks and enforcement."""
        budget_check = check_budget(self.context_budget, self.tokens_dir)
        if not budget_check["allowed"]:
            actions = enforce_budget(self.context_budget, self.tokens_dir)
            if actions:
                LOGGER.info(
                    "Context budget enforced for sheet %s (%s): %s",
                    self.sheet_id,
                    budget_check["reason"],
                    ", ".join(actions),
                )
            else:
                LOGGER.warning(
                    "Context budget exceeded for sheet %s (%s); no tokens removed",
                    self.sheet_id,
                    budget_check["reason"],
                )

            budget_check = check_budget(self.context_budget, self.tokens_dir)
            if not budget_check["allowed"]:
                raise RuntimeError(
                    "Context budget still exceeded for sheet {}: {}".format(
                        self.sheet_id,
                        budget_check["reason"],
                    )
                )

        writer = self.token_writer or self.emit_token
        token_id = writer(token_payload) if writer else None
        register_token(self.context_budget, token_id)
        return token_id


def apply_policy_gate_for_cue(cue: dict[str, Any], policies_dir: str | Path) -> bool:
    """Return True when a cue can proceed, False when it should be skipped."""
    gate_config = cue.get("policy_gate")
    if not gate_config:
        return True

    result = evaluate_gate(gate_config.get("require") or [], policies_dir)

    try:
        should_proceed = enforce_gate(gate_config, policies_dir)
    except PolicyViolation:
        raise

    if not should_proceed:
        cue_id = cue.get("id", "unknown")
        LOGGER.warning("[cuesheet] policy gate blocked cue '%s': %s", cue_id, result.get("missing", []))

    return should_proceed


def load_sheet_with_trust(sheet_path, available_tools=None, request_context=None):
    """Load a CueSheet file and enforce trust scope once for the session."""
    import yaml
    with open(sheet_path, "r", encoding="utf-8") as handle:
        sheet = yaml.safe_load(handle) or {}

    trust_config = sheet.get("trust") or {}
    context = dict(request_context or {})
    context["available_tools"] = list(available_tools or [])

    trust_result = evaluate_trust(trust_config, context)
    if not trust_result["allowed"]:
        raise PermissionError(trust_result["reason"])

    return {
        "sheet": sheet,
        "execution_context": {
            "authenticated": bool(context.get("authenticated", False)),
            "tenant_id": context.get("tenant_id"),
            "allowed_tools": trust_result["effective_tools"],
        },
    }


class HookAwareCueSheetExecutor:
    """Executor mixin that manages token lifecycle hooks via polling watcher."""

    def __init__(self, tokens_dir=None):
        self.tokens_dir = tokens_dir or os.path.join(".claude", "tokens")
        self._hook_watcher = None
        self._hooks = []
        self._hook_action_queue = queue.Queue()
        self._heated_cues = []

    def start_session(self, cuesheet_config):
        hooks_config = (cuesheet_config or {}).get("hooks")
        self._hooks = compile_hooks(hooks_config)

        if not self._hooks:
            return

        self._hook_watcher = watch_tokens(self.tokens_dir, self._hooks, poll_interval=5)

    def stop_session(self):
        if self._hook_watcher:
            self._collect_watcher_actions()
            self._hook_watcher["stop"]()
            self._hook_watcher = None

    def tick(self):
        self._collect_watcher_actions()
        self._drain_hook_actions()

    def _collect_watcher_actions(self):
        if not self._hook_watcher:
            return
        for action in self._hook_watcher["pop_actions"]():
            self._hook_action_queue.put(action)

    def _drain_hook_actions(self):
        while not self._hook_action_queue.empty():
            action_item = self._hook_action_queue.get_nowait()
            execute_hook_action(
                action_item.get("action"),
                action_item.get("params") or {},
                {
                    "tokens_dir": self.tokens_dir,
                    "queue_cue": self._queue_cue,
                },
            )

    def _queue_cue(self, cue_id):
        self._heated_cues.append(cue_id)

    @property
    def heated_cues(self):
        return list(self._heated_cues)
