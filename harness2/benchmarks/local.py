# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Local adapter: run Harness² on your own folder of tasks (see docs/LOCAL.md).

The folder holds `<domain>/<task>/task.md`, plus an optional `files/` directory and an optional
`task.toml` (`outputs` and `verify`). Every rollout works in its own copy of `files/`. The
`verify` command runs only from `score()`, which only `core.judge` calls after selection has
committed, so its result cannot reach the proposer or the selector.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import config
from ..core import runmark as RM
from ..core import tree as HT
from ..core.spec import BenchPrompts, BenchSpec, resolve_channel_model
from ..substrate.base import Install, RunRequest
from ..substrate.opencode import OpencodeSubstrate
from .jobbench import OC_BASE_HARNESS, render_opencode_tree
from .tasks import select

DEFAULT_WORK = (
    "Work a practitioner asks for, done against the files they supply and handed back as "
    "files they can use without repair."
)
BENCH_KEYS = {"work", "domain_word", "deliverable_word"}
TASK_KEYS = {"outputs", "verify"}

_DEFAULT_TIMEOUT = 2400
# ponytail: one fixed ceiling for the verify command; add a `verify_timeout` key to task.toml
# if a build (a cold Swift build, say) needs longer.
VERIFY_TIMEOUT = 1800
_MAX_CHARS_PER_FILE = 60_000


@dataclass(frozen=True)
class Task:
    dir: Path
    prompt: str
    outputs: tuple[str, ...]
    verify: str | None


def _toml(path: Path, allowed: set[str]) -> dict:
    """The table in `path`, or {} when it does not exist. Unknown keys are typos."""
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from None
    if unknown := sorted(set(data) - allowed):
        raise ValueError(f"{path}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
    return data


def _relative(path: str, where: Path) -> str:
    p = Path(path)
    if not path or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"{where}: output path {path!r} must be relative and stay inside files/")
    return p.as_posix()


def _files_under(root: Path, rel: str) -> list[Path]:
    """Files at `rel` (a file or a directory) that really live under `root`.

    The agent controls this tree, so a symlink that points outside it is not a deliverable.
    """
    p = root / rel
    found = [p] if p.is_file() else sorted(p.rglob("*")) if p.is_dir() else []
    top = root.resolve()
    return [f for f in found if f.is_file() and f.resolve().is_relative_to(top)]


def _run(cmd, *, timeout: int, **kw) -> int | None:
    """Run `cmd` in its own process group; on a wall timeout kill the group and return None.

    Python does the timing, so nothing depends on GNU `timeout` (absent on macOS).
    """
    proc = subprocess.Popen(cmd, start_new_session=True, **kw)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()
        return None


class LocalAdapter:
    """The BenchmarkAdapter surface for a folder of your own tasks, on the OpenCode substrate."""

    def __init__(
        self,
        tasks_dir: Path,
        *,
        base_harness: Path | None = None,
        timeout: int | None = None,
        split_file: Path | None = None,
        runs_root: Path | None = None,
    ):
        self.tasks_dir = Path(tasks_dir).expanduser().resolve()
        if not self.tasks_dir.is_dir():
            raise FileNotFoundError(f"--tasks-dir {tasks_dir} is not a directory")
        self._base = Path(base_harness).expanduser().resolve() if base_harness else None
        self.split_file = split_file
        self.timeout = int(timeout or _DEFAULT_TIMEOUT)
        self.substrate = OpencodeSubstrate(launcher=self._launch)
        channel = resolve_channel_model(None, None, effort="high")
        prompts = self._prompts()
        self.spec = BenchSpec(
            name=f"local-{self._namespace_hash()}",
            runs_root=Path(runs_root).expanduser().resolve()
            if runs_root
            else config.RUNS / "local",
            install_mode="workspace_agents_md",
            prompts=prompts,
            channels={"proposer": channel, "selector": channel},
            domain_word=prompts.domain_word,
            leak_globs=("task.toml",),
            allow_subagents=True,
        )
        self._results_root = self.spec.runs_root / "results"

    def _namespace_hash(self) -> str:
        """Two folders never share a namespace or a library. A custom base harness is the
        baseline every base rollout runs on, so its content is part of the identity too."""
        key = str(self.tasks_dir)
        if self._base is not None:
            key += "|" + RM.fingerprint(self._base)
        return hashlib.sha1(key.encode()).hexdigest()[:8]

    def _prompts(self) -> BenchPrompts:
        path = self.tasks_dir / "bench.toml"
        data = _toml(path, BENCH_KEYS)
        if not all(isinstance(v, str) for v in data.values()):
            raise ValueError(f"{path}: {sorted(BENCH_KEYS)} must be strings")
        try:
            return BenchPrompts(
                work=data.get("work") or DEFAULT_WORK,
                domain_word=data.get("domain_word") or "domain",
                deliverable_word=data.get("deliverable_word") or "deliverable",
            )
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from None

    def _task(self, task_id: str) -> Task:
        if ".." in Path(task_id).parts or task_id.startswith("/"):
            raise ValueError(f"malformed local task id {task_id!r} (expected '<domain>/<task>')")
        d = self.tasks_dir / task_id
        if not (d / "task.md").is_file():
            raise FileNotFoundError(f"task instructions missing: {d / 'task.md'}")
        meta = _toml(d / "task.toml", TASK_KEYS)
        outputs = meta.get("outputs") or []
        if not isinstance(outputs, list) or not all(isinstance(o, str) for o in outputs):
            raise ValueError(f"{d / 'task.toml'}: outputs must be a list of paths")
        outputs = tuple(_relative(o, d / "task.toml") for o in outputs)
        verify = meta.get("verify")
        if verify is not None and not isinstance(verify, str):
            raise ValueError(f"{d / 'task.toml'}: verify must be one command string")
        prompt = (d / "task.md").read_text(encoding="utf-8").strip()
        if outputs:
            prompt += "\n\n## Output files\nSave the result in these files, inside `files/`:\n"
            prompt += "".join(f"- `{o}`\n" for o in outputs)
        return Task(d, prompt, outputs, (verify or "").strip() or None)

    def tasks(self, split: str = "all") -> list[str]:
        found = {
            p.parent.relative_to(self.tasks_dir).as_posix(): p.parts[-3]
            for p in sorted(self.tasks_dir.glob("*/*/task.md"))
        }
        if not found:
            raise FileNotFoundError(
                f"No tasks under {self.tasks_dir}: expected <domain>/<task>/task.md (docs/LOCAL.md)"
            )
        for task_id in found:
            self._task(task_id)
        return select(found, split, self.split_file, benchmark=self.spec.name)

    def domain_of(self, task_id: str) -> str:
        return task_id.split("/", 1)[0]

    def task_prompt(self, task_id: str) -> str:
        """task.md plus the output paths: what the agent was told, and nothing about `verify`."""
        return self._task(task_id).prompt

    def task_files(self, task_id: str) -> Path | None:
        d = self._task(task_id).dir / "files"
        return d if d.is_dir() else None

    def _run_dir(self, run_id: str) -> Path:
        out = (self._results_root / run_id).resolve()
        if not out.is_relative_to(self._results_root):
            raise ValueError(f"run id {run_id!r} escapes the results folder")
        return out

    def execute(
        self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None
    ) -> Path:
        out = self._run_dir(run_id)
        inputs = RM.inputs_fingerprint(self.task_prompt(task_id), self.task_files(task_id))
        reusable = RM.is_complete(out, harness_dir=harness_dir, inputs=inputs)
        if reusable and self.artifacts_intact(out):
            return out
        if out.is_dir():
            reason = RM.why_not(out, harness_dir=harness_dir, inputs=inputs)
            if reason == "reusable":
                reason = "output fingerprint missing or changed"
            print(f"  [local-oc] re-rolling {run_id}: {reason}", flush=True)
            shutil.rmtree(out)
        src = Path(harness_dir) if harness_dir is not None else self.base_harness()
        install = self.substrate.materialise(src, self.spec, {})
        try:
            self.substrate.execute(task_id, install, [run_id], int(timeout or self.timeout))
        finally:
            shutil.rmtree(install.staging_dir, ignore_errors=True)
        RM.mark_complete(
            out,
            run_id=run_id,
            harness_dir=harness_dir,
            inputs=inputs,
            extra={"output_fingerprint": self._output_fingerprint(out)},
        )
        return out

    def _launch(self, req: RunRequest, install: Install) -> list[Path]:
        """Stage one workspace, run the agent in it, keep its event stream."""
        if len(req.run_ids) != 1:
            raise ValueError(f"local rolls one run per call, got {len(req.run_ids)} run ids")
        task_id = req.task_ref
        run_dir = self._run_dir(req.run_ids[0])
        workspace = run_dir / "workspace"
        ctx = {
            "env_description": "A command-line workspace; the task files are in files/.",
            "domain_focus": self.domain_of(task_id),
            "date": datetime.now(timezone.utc).date().isoformat(),
            "working_directory": "the workspace (task files in files/)",
        }
        render_opencode_tree(install.staging_dir, workspace, ctx)
        source = self.task_files(task_id)
        if source is not None:
            shutil.copytree(source, workspace / "files")
        else:
            (workspace / "files").mkdir()
        (run_dir / "task_id").write_text(task_id, encoding="utf-8")
        prompt = (
            self.task_prompt(task_id)
            + "\n\n---\nThe task files are in `files/` under the current directory. Edit them in "
            "place or add new files there. Do not read or write outside the current directory."
        )
        self._run_agent(prompt, workspace, run_dir, req.timeout)
        return [run_dir]

    def _run_agent(self, prompt: str, workspace: Path, run_dir: Path, timeout: int) -> None:
        """Run OpenCode once in `workspace`. The only method that needs bun and a model."""
        self.substrate.preflight()
        session, log = run_dir / "opencode_session.jsonl", run_dir / "opencode.log"
        argv = self.substrate.run_argv(
            prompt, workspace, config.SOLVER_MODEL_OC, title=run_dir.name
        )
        env = self.substrate.run_env(os.environ, workspace)
        with session.open("w", encoding="utf-8") as out, log.open("w", encoding="utf-8") as err:
            code = _run(
                argv,
                cwd=config.OPENCODE_DIR,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=timeout,
            )
        if code is None:
            print(f"  [local-oc] wall-timeout after {timeout}s: {run_dir.name}", flush=True)
        elif code != 0:
            raise RuntimeError(f"OpenCode exited with status {code}; see {log}")
        if not any(ln.startswith("{") for ln in session.read_text(errors="replace").splitlines()):
            raise RuntimeError(
                f"OpenCode produced no events for {run_dir.name} (credentials, model or bun?); "
                f"see {log}. Left unmarked so the next attempt re-rolls it."
            )

    def _files(self, run_dir: Path) -> dict[str, Path]:
        """What the rollout produced, by path inside `files/`: the listed outputs or, when the
        task lists none, the UTF-8 text files it added or changed."""
        run_dir = Path(run_dir)
        task_id = (run_dir / "task_id").read_text(encoding="utf-8")
        task, root = self._task(task_id), run_dir / "workspace" / "files"
        if task.outputs:
            paths = [f for rel in task.outputs for f in _files_under(root, rel)]
        else:
            source, paths = self.task_files(task_id), []
            for f in _files_under(root, "."):
                rel = f.relative_to(root)
                if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                    continue
                body = f.read_bytes()
                try:
                    body.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                before = source / rel if source else None
                if before is None or not before.is_file() or before.read_bytes() != body:
                    paths.append(f)
        return {f.relative_to(root).as_posix(): f for f in paths}

    def delivered(self, run_dir: Path) -> bool:
        task_id = (Path(run_dir) / "task_id").read_text(encoding="utf-8")
        outputs = self._task(task_id).outputs
        if outputs:
            root = Path(run_dir) / "workspace" / "files"
            return all(_files_under(root, rel) for rel in outputs)
        return bool(self._files(run_dir))

    def _output_fingerprint(self, run_dir: Path) -> str | None:
        if not self.delivered(run_dir):
            return None
        h = hashlib.sha1()
        for rel, f in sorted(self._files(run_dir).items()):
            h.update(rel.encode() + b"\0" + f.read_bytes() + b"\0")
        return h.hexdigest()[:16]

    def artifacts_intact(self, run_dir: Path) -> bool:
        marker = RM.read_mark(run_dir) or {}
        return "output_fingerprint" in marker and marker[
            "output_fingerprint"
        ] == self._output_fingerprint(run_dir)

    # ponytail: rollout_cost(self, run_dir) goes here once PR #127 merges. It prices the stream
    # that trajectory() already reads, run_dir / "opencode_session.jsonl", with
    # self.substrate.stream_cost([...]). Not built: nothing in core calls the hook before then.

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        stream = Path(run_dir) / "opencode_session.jsonl"
        if not stream.is_file():
            return f"(no trajectory recorded for {Path(run_dir).name})"
        return self.substrate.read_trajectory(stream, obs_trunc=obs_trunc, max_chars=10**9)

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        files = {}
        for rel, f in self._files(run_dir).items():
            text = f.read_text(encoding="utf-8", errors="replace")
            if len(text) > _MAX_CHARS_PER_FILE:
                text = text[:_MAX_CHARS_PER_FILE] + "\n…(truncated)"
            files[rel] = text
        return files

    def deliverable_text(self, run_dir: Path) -> str:
        files = self.deliverable_files(run_dir)
        if not files:
            return "(this run produced NO deliverables)"
        return "\n\n".join(
            f"----- FILE: {name} -----\n{text.strip() or '(empty file)'}"
            for name, text in files.items()
        )

    def base_harness(self) -> Path:
        """`--base-harness`, else the bare stock-OpenCode baseline JobBench ships."""
        base = self._base or OC_BASE_HARNESS
        if not HT.is_tree(self.substrate.surface(), base):
            raise FileNotFoundError(
                f"base harness {base} is not an {self.substrate.surface().name} tree (no "
                f"always-on file such as systemprompt.md); fix --base-harness"
            )
        return base

    def validate_tree(self, tree: dict[str, str]) -> None:
        """A local task adds no environment invariant beyond the framework rules."""
        return None

    def preflight(self) -> None:
        self.base_harness()

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        """Run the task's `verify` command on a copy of this rollout's files.

        Exit 0 is a pass and any other exit is a fail. A command that could not run at all
        (timed out, not found, not executable) is None: the check failed, not the work.
        No `verify` means nothing to judge, also None. The report is written outside the
        run directory, which `trajectory()` and `deliverable_files()` read from.
        """
        task = self._task(task_id)
        if not task.verify:
            return None
        run_dir = Path(run_dir)
        report = (
            self.spec.runs_root
            / "verify"
            / (run_dir.relative_to(self._results_root).as_posix().replace("/", "__") + ".log")
        )
        report.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="local-verify-") as tmp:
            copy = Path(tmp) / "files"
            shutil.copytree(run_dir / "workspace" / "files", copy, symlinks=True)
            with report.open("w", encoding="utf-8") as out:
                out.write(f"$ {task.verify}\n")
                out.flush()
                code = _run(
                    task.verify,
                    shell=True,
                    cwd=copy,
                    env={**config.scrub_env(), "TASK_DIR": str(task.dir)},
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    timeout=VERIFY_TIMEOUT,
                )
                out.write(f"\n[exit status: {'timed out' if code is None else code}]\n")
        if code is None or code in (126, 127):
            return None
        return {"n_passed": int(code == 0), "n_criteria": 1, "all_pass": code == 0}


__all__ = ["LocalAdapter", "DEFAULT_WORK"]
