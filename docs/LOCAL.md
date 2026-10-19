# Run Harness² on your own tasks

`--bench local` reads tasks from a folder you give it, so you can use the method on your own
work without writing an adapter. It runs on the OpenCode executor. Complete the
[installation and credentials](SETUP.md) first. OpenCode comes from `harness2-setup`, which
installs it for whichever benchmark you name (`harness2-setup lab`, for example); local needs
no benchmark data of its own. Reading `task.yaml` and `bench.yaml` needs PyYAML:

```bash
uv pip install -e '.[local]'
```

## Task folder

```
my-tasks/
  bench.yaml                  # optional
  <domain>/                   # the grouping unit: the library grows per domain
    <task>/
      task.md                 # the task, as the agent should receive it
      files/                  # optional: source material, copied into each rollout
      task.yaml               # optional: outputs and verify
      check.py                # anything else you keep here stays private
```

Pass the folder that holds the domains (`my-tasks` above) to `--tasks-dir`. A task id is
`<domain>/<task>`.

**task.md** is the whole task. Write it the way you would brief a colleague.

**files/** is the source material and, for coding tasks, the project itself. Every rollout gets
its own fresh copy, and the agent works in that copy. One rollout never sees the edits of another.
Put nothing private in `files/`: it is copied to the agent's
workspace and shown to the improver as source material.

**task.yaml** has two optional keys:

```yaml
outputs:                  # paths inside files/ that the task should produce
  - Sources/Parser.swift
verify: swift test        # a shell command; exit status 0 means pass
```

- `outputs` are told to the agent in the prompt. A rollout that does not write all of them
  counts as not delivered. A listed folder counts when it holds a file. Without `outputs`, the
  deliverable is every text file the rollout added or changed in `files/` (hidden folders such
  as `.build` are skipped).
- `verify` is never shown to the agent or to the improver. It runs once, on a copy of the rollout's
  `files/`, after selection has chosen a rollout. Keep the command and any private tests next to
  `task.md`, not in `files/`. The command can find them through `$TASK_DIR`, the absolute path of the
  task folder: `verify: python3 "$TASK_DIR/check.py"`.
  This is YAML, so put a command in single quotes when it contains `: ` or starts with a quote.

**bench.yaml** sets the words the shared prompts use. All keys are optional:

```yaml
work: review commercial contracts for a small company's legal team
domain_word: contract type
deliverable_word: review memo
```

`work` must describe the work, not how it is judged: the words benchmark, evaluation, grader,
rubric and score are rejected.

## Run

```bash
harness2 --bench local --tasks-dir ./my-tasks --k 1 --mode parallel --dry-run
harness2 --bench local --tasks-dir ./my-tasks --k 1 --mode parallel
harness2-judge --bench local --tasks-dir ./my-tasks --k 1 --mode parallel
harness2-report --bench local --tasks-dir ./my-tasks --k 1 --mode parallel
```

Each model-backed run costs money; start with `--max-tasks 1` or `--only-domains`.
Models stay in `.env`, as for the other benchmarks. Use the same `--tasks-dir` for the run,
the judge and the report.

`--base-harness DIR` starts from your own c1..c8 tree instead of the bare OpenCode baseline.
Each different tasks folder, and each different base harness, gets its own namespace and
library under `runs/local/`, so they never mix.

## What the judge does

`harness2-judge` runs each task's `verify` command on the rollout the method selected.
Exit status 0 is one criterion passed, and any other exit status is one criterion failed. A task
without `verify` is not graded, which is not the same as a failure. A command that cannot run at all
(timeout, command not found) is also not graded. The command's output is saved to
`runs/local/verify/`, outside the rollout directories that the improver reads.

The agent may run its own tests while it works. Say so in `task.md`, and those runs show up in its
trajectory like any other command. The `verify` command is the one only the judge runs.

## Notes

- The rollout runs on the machine you start it from, and so does `verify`. Install what
  your tasks need (a Swift toolchain, for example) there. The timeouts use Python, not GNU
  `timeout`, so they work on macOS.
- `verify` stops after 30 minutes, and a rollout after 40 minutes.
- Editing a task after its rollouts finished does not re-roll them. Use a new `HARNESS2_RUNS_DIR`.
