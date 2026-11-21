"""Executors. Dry-run is the default everywhere; real writes need execute=True + confirmation."""
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .planner import Plan
from .runner import DEFAULT_RUNNER, CommandError
from .safety import SafetyError, check_device_target, check_image_target


class ConfirmationError(RuntimeError):
    pass


@dataclass
class ExecResult:
    executed: bool
    log: List[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""


def privilege_prefix(method: Optional[str]) -> List[str]:
    """Explicit user-approved elevation only. None => run as current user."""
    if not method or os.geteuid() == 0:
        return []
    if method == "sudo":
        return ["sudo"]
    if method == "pkexec":
        return ["pkexec"]
    raise ValueError(f"unknown privilege method {method}")


class BaseExecutor:
    name = "base"

    def run_plan(self, plan: Plan, **kw) -> ExecResult:
        raise NotImplementedError


class MockExecutor(BaseExecutor):
    """Records the plan without running anything (used by tests and previews)."""
    name = "mock"

    def __init__(self, fail_at: Optional[int] = None):
        self.fail_at, self.ran = fail_at, []

    def run_plan(self, plan, guard: Callable = None, **kw):
        res = ExecResult(executed=True)
        for i, s in enumerate(plan.steps, 1):
            if s.writes and guard:
                try:
                    guard(i)
                except SafetyError as e:
                    return ExecResult(True, res.log + [f"ABORT before step {i}: {e}"], False, str(e))
            if self.fail_at == i:
                return ExecResult(True, res.log + [f"FAILED step {i}"], False, f"step {i} failed")
            self.ran.append(s.argv)
            res.log.append(f"mock-ran {i}")
        return res


class LinuxExecutor(BaseExecutor):
    name = "linux"

    def __init__(self, runner=DEFAULT_RUNNER):
        self.runner = runner

    def run_plan(self, plan: Plan, execute: bool = False, confirmation: Optional[str] = None,
                 expected_token: Optional[str] = None, guard: Callable = None,
                 privilege: Optional[str] = None) -> ExecResult:
        if not execute:
            return ExecResult(False, ["DRY RUN - nothing executed. Plan:"] + plan.render().splitlines())
        if not expected_token or confirmation != expected_token:
            raise ConfirmationError(f"typed confirmation does not match '{expected_token}'; nothing done")
        if plan.kind == "device":
            check_device_target(plan.target)
            if guard is None:
                raise SafetyError("device execution requires a revalidation guard")
        else:
            check_image_target(plan.target)
        pre = privilege_prefix(privilege) if plan.kind == "device" else []
        res = ExecResult(True)
        for i, s in enumerate(plan.steps, 1):
            try:
                if s.writes and guard:  # TOCTOU: identity/mount (+layout before step 1) re-checked
                    guard(i)
                self.runner.run(pre + s.argv)
                res.log.append(f"ok {i}: {s}")
            except (CommandError, SafetyError) as e:
                res.ok, res.error = False, str(e)
                res.log.append(f"ABORTED at step {i}: {e}")
                res.log.append("No further commands were run. If partitioning had started the drive may be "
                               "partially prepared and is NOT valid; re-run detection and preparation.")
                break
        return res
