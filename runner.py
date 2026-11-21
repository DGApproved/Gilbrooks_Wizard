"""Single choke point for running external commands (injectable for tests)."""
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional


class CommandError(RuntimeError):
    pass


@dataclass
class Result:
    argv: List[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


# Callables invoked with argv before every real command; may raise to refuse.
GUARD_HOOKS: List[Callable[[List[str]], None]] = []


class Runner:
    """Runs real commands. Every call passes GUARD_HOOKS first."""

    def run(self, argv: List[str], check: bool = True, input: Optional[str] = None,
            timeout: int = 600) -> Result:
        for hook in GUARD_HOOKS:
            hook(list(argv))
        try:
            p = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise CommandError(f"command not found: {argv[0]}") from e
        res = Result(list(argv), p.returncode, p.stdout, p.stderr)
        if check and p.returncode != 0:
            raise CommandError(f"`{shlex.join(argv)}` failed ({p.returncode}): {p.stderr.strip()}")
        return res


@dataclass
class MockRunner(Runner):
    """Returns canned responses keyed by command prefix; records every call."""
    responses: dict = field(default_factory=dict)  # "cmd arg" -> Result | (rc, out, err)
    calls: List[List[str]] = field(default_factory=list)
    default: tuple = (0, "", "")

    def run(self, argv, check=True, input=None, timeout=600):
        self.calls.append(list(argv))
        joined = shlex.join(argv)
        best = None
        for key, val in self.responses.items():
            if joined.startswith(key) and (best is None or len(key) > len(best[0])):
                best = (key, val)
        rc, out, err = best[1] if best else self.default
        if callable(rc):
            rc, out, err = rc(argv)
        res = Result(list(argv), rc, out, err)
        if check and rc != 0:
            raise CommandError(f"`{joined}` failed ({rc}): {err}")
        return res


DEFAULT_RUNNER = Runner()
