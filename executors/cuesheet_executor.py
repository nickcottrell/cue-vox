"""
Cue-Sheet Executor - Execute compiled cue-sheets.

Runs operations from a compiled cue-sheet (produced by CuesheetCompiler),
emitting progress events via Socket.IO and creating result tokens.

Callable from:
- Socket.IO (live UI execution via cuesheet_execute event)
- CLI (./hooks run <cuesheet.yaml>)
"""

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Callable


def _compute_source_hash(yaml_path):
    """SHA-256 of raw cue-sheet YAML bytes."""
    with open(yaml_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class CuesheetExecutor:
    """
    Executes compiled cue-sheets operation by operation.

    Each operation runs based on its type:
    - run_report: Execute a shell command and capture output
    - cue_dispatch: Send a cue to CueSync via the dispatcher
    - run_command: Execute a command in the maestro root
    """

    def __init__(self, maestro_root=None, dispatcher_path=None,
                 token_factory=None, emit_fn=None, yaml_path=None):
        """
        Args:
            maestro_root: Path to maestro root directory
            dispatcher_path: Path to cue-dispatcher/dispatch.py
            token_factory: TokenFactory instance for creating result tokens
            emit_fn: Socket.IO emit function for progress events
            yaml_path: Path to source YAML file (for source_hash computation)
        """
        self.maestro_root = Path(maestro_root) if maestro_root else Path.cwd()
        self.dispatcher_path = dispatcher_path
        self.token_factory = token_factory
        self.emit_fn = emit_fn
        self.yaml_path = yaml_path

    def execute(self, cuesheet):
        """
        Execute a compiled cue-sheet.

        Args:
            cuesheet: Dict from CuesheetCompiler.compile()
                {title, doc_id, version, operations: [{name, type, params}]}

        Returns:
            Dict with execution results:
            {
                "title": str,
                "doc_id": str,
                "status": "completed" | "failed",
                "operations": [{name, status, output, duration_ms}],
                "total_duration_ms": int
            }
        """
        title = cuesheet.get("title", "Untitled")
        doc_id = cuesheet.get("doc_id", "unknown")
        operations = cuesheet.get("operations", [])

        self._emit_progress("cuesheet_start", {
            "title": title,
            "doc_id": doc_id,
            "total_operations": len(operations)
        })

        results = []
        overall_start = time.time()
        overall_status = "completed"

        for i, op in enumerate(operations):
            op_name = op.get("name", "Operation %d" % (i + 1))
            op_type = op.get("type", "unknown")
            params = op.get("params", {})

            self._emit_progress("operation_start", {
                "index": i,
                "name": op_name,
                "type": op_type
            })

            op_start = time.time()

            try:
                output = self._run_operation(op_type, params)
                status = "completed"
            except Exception as e:
                output = str(e)
                status = "failed"
                overall_status = "failed"

            duration_ms = int((time.time() - op_start) * 1000)

            result = {
                "name": op_name,
                "type": op_type,
                "status": status,
                "output": output,
                "duration_ms": duration_ms
            }
            results.append(result)

            self._emit_progress("operation_complete", {
                "index": i,
                "name": op_name,
                "status": status,
                "duration_ms": duration_ms
            })

        total_duration = int((time.time() - overall_start) * 1000)

        # Compute source hash if yaml_path available
        source_hash = ""
        if self.yaml_path and Path(self.yaml_path).exists():
            source_hash = _compute_source_hash(self.yaml_path)

        execution_result = {
            "title": title,
            "doc_id": doc_id,
            "status": overall_status,
            "operations": results,
            "total_duration_ms": total_duration,
            "source_hash": source_hash,
        }

        self._emit_progress("cuesheet_complete", {
            "title": title,
            "status": overall_status,
            "total_duration_ms": total_duration
        })

        # Create ephemeral result token
        result_token_id = ""
        if self.token_factory:
            result_token_id = self.token_factory.create(
                token_type="cuesheet_result",
                label="cuesheet_%s" % doc_id,
                value="%s: %s (%dms)" % (title, overall_status, total_duration),
                thermal={"weight": "ephemeral"},
                extra_fields={
                    "doc_id": doc_id,
                    "status": overall_status,
                    "operation_count": len(results),
                    "total_duration_ms": total_duration,
                    "source_hash": source_hash,
                }
            ) or ""

        execution_result["result_token_id"] = result_token_id

        # Emit signoff gate request
        self._emit_progress("cuesheet_signoff", {
            "title": title,
            "doc_id": doc_id,
            "source_hash": source_hash,
            "result_token_id": result_token_id,
            "status": overall_status,
        })

        return execution_result

    def _run_operation(self, op_type, params):
        """
        Execute a single operation based on its type.

        Args:
            op_type: Operation type string
            params: Operation parameters dict

        Returns:
            Output string from execution
        """
        handlers = {
            "run_report": self._handle_run_report,
            "run_command": self._handle_run_command,
            "cue_dispatch": self._handle_cue_dispatch
        }

        handler = handlers.get(op_type)
        if not handler:
            return "Unknown operation type: %s" % op_type

        return handler(params)

    def _handle_run_report(self, params):
        """Execute a command and return its output."""
        command = params.get("command", "")
        if not command:
            return "No command specified"

        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=str(self.maestro_root), timeout=30
        )

        if result.returncode != 0:
            return "Exit code %d: %s" % (result.returncode, result.stderr.strip())

        return result.stdout.strip()

    def _handle_run_command(self, params):
        """Execute a command (same as run_report for now)."""
        return self._handle_run_report(params)

    def _handle_cue_dispatch(self, params):
        """Dispatch a cue to CueSync."""
        tool = params.get("tool", "")
        payload = params.get("payload", {})

        if not tool:
            return "No tool specified for cue_dispatch"

        if not self.dispatcher_path:
            return "No dispatcher configured"

        dispatcher = Path(self.dispatcher_path)
        if not dispatcher.exists():
            return "Dispatcher not found at %s" % dispatcher

        cue = {
            "cue_id": "cuesheet_%d" % int(time.time()),
            "tool": tool,
            "payload": payload,
            "metadata": {
                "dispatched_at": datetime.now().isoformat(),
                "dispatcher": "cuesheet_executor"
            }
        }

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cue, f)
            cue_file = f.name

        try:
            result = subprocess.run(
                [sys.executable, str(dispatcher), cue_file],
                capture_output=True, text=True, timeout=10
            )

            Path(cue_file).unlink(missing_ok=True)

            if result.returncode == 0:
                return "Dispatched: %s" % result.stdout.strip()
            else:
                return "Failed: %s" % result.stderr.strip()
        except Exception as e:
            Path(cue_file).unlink(missing_ok=True)
            raise e

    def _emit_progress(self, event_type, data):
        """Emit a progress event via Socket.IO."""
        if self.emit_fn:
            self.emit_fn("cuesheet_progress", {
                "event": event_type,
                **data
            })
