"""Oracles: checks that say whether a bug shows on a PDF.

An oracle runs the software under test in a child process, so that a crash or a
hang in the target cannot take docmutant down with it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Outcome(str, Enum):
    FIRES = "fires"
    SILENT = "silent"


class OracleKind(str, Enum):
    CRASH = "crash"
    """Fires when the target is killed by a signal, for example SIGSEGV."""

    HANG = "hang"
    """Fires when the target does not finish within the timeout."""

    ERROR = "error"
    """Fires when the target exits with any nonzero status, including a signal."""


@dataclass(frozen=True)
class CheckResult:
    outcome: Outcome
    returncode: int | None
    stderr_tail: str = ""


@dataclass
class CommandOracle:
    """Runs ``argv`` with ``{pdf}`` and ``{page}`` replaced by the file and page to check."""

    kind: OracleKind
    argv: list[str]
    target: str
    timeout: float = 60.0
    cwd: Path | None = None
    calls: int = field(default=0, init=False)

    @property
    def name(self) -> str:
        return self.kind.value

    def check(self, pdf: Path, page: int = 1) -> CheckResult:
        self.calls += 1
        argv = [arg.replace("{pdf}", str(pdf)).replace("{page}", str(page)) for arg in self.argv]
        try:
            completed = subprocess.run(
                argv,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            outcome = Outcome.FIRES if self.kind is OracleKind.HANG else Outcome.SILENT
            return CheckResult(outcome, None, "timeout")

        code = completed.returncode
        if self.kind is OracleKind.CRASH:
            fires = code < 0
        elif self.kind is OracleKind.ERROR:
            fires = code != 0
        else:
            fires = False
        return CheckResult(
            Outcome.FIRES if fires else Outcome.SILENT, code, completed.stderr[-2000:]
        )


def target_script(name: str) -> Path:
    """Path of a bundled standalone target script, to be run by the target's Python."""
    path = Path(__file__).parent / "targets" / f"{name}.py"
    if not path.is_file():
        raise FileNotFoundError(f"no bundled target script named {name!r}")
    return path


def docling_parse_render_oracle(
    kind: OracleKind,
    python: Path,
    *,
    scale: float,
    timeout: float = 60.0,
    label: str | None = None,
) -> CommandOracle:
    """An oracle that renders one page with docling-parse under ``python``."""
    if label is None:
        label = _docling_parse_label(python)
    argv = [
        str(python),
        "-I",
        str(target_script("docling_parse_render")),
        "{pdf}",
        "--page",
        "{page}",
        "--scale",
        str(scale),
    ]
    return CommandOracle(kind=kind, argv=argv, target=label, timeout=timeout)


def _docling_parse_label(python: Path) -> str:
    completed = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import importlib.metadata as m; print(m.version('docling-parse'))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    # No interpreter path: the label goes into published manifests.
    return f"docling-parse {completed.stdout.strip()}"
