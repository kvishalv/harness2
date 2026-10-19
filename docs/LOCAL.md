# Run Harness² on your own tasks

`--bench local` reads tasks from a folder you give it, so you can use the method on your own
work without writing an adapter. It runs on the OpenCode executor. Complete the
[installation and credentials](SETUP.md) first. OpenCode comes from `harness2-setup`, which
installs it for whichever benchmark you name (`harness2-setup lab`, for example); local needs
no benchmark data of its own.

## Task folder

```
my-tasks/
  bench.toml                  # optional
  <domain>/                   # the grouping unit: the library grows per domain
    <task>/
      task.md                 # the task, as the agent should receive it
      files/                  # optional: source material, copied into each rollout
      task.toml               # optional: outputs and verify
      check.py                # anything else here is not given to the agent
```

Pass the folder that holds the domains (`my-tasks` above) to `--tasks-dir`. A task id is
`<domain>/<task>`.

**task.md** is the whole task. Write it the way you would brief a colleague.

**files/** is the source material and, for coding tasks, the project itself. Every rollout gets
its own fresh copy, and the agent works in that copy. One rollout never sees the edits of another.
Everything in `files/` is copied into the agent's workspace and shown to the improver as source
material, so put the verify command and its test files next to `task.md`, outside `files/`.

Files outside `files/` are not given to the agent. They sit outside its workspace, and OpenCode's
folder rule keeps the agent inside the workspace. That rule is not a sandbox: a shell command
that names the right path can still read them, so do not keep secrets in the tasks folder.

**task.toml** has two optional keys:

```toml
outputs = ["Sources/Parser.swift"]   # paths inside files/ that the task should produce
verify = "swift test"                # a shell command; exit status 0 means pass
```

A multi-line command is a TOML `'''` string. It is a shell script, so start it with `set -e`
when any failing line should fail the check:

```toml
verify = '''
set -e
python3 "$TASK_DIR/check.py" "$TASK_DIR/expected.txt"
swift test --filter ParserTests
'''
```

- `outputs` are told to the agent in the prompt. A rollout that does not write all of them
  counts as not delivered. A listed folder counts when it holds a file. Without `outputs`, the
  deliverable is every UTF-8 text file the rollout added or changed in `files/` (binary files and
  hidden folders such as `.build` are skipped).
- `verify` is never shown to the agent or to the improver. It runs once, on a copy of the
  rollout's `files/`, after selection has chosen a rollout. The command finds the files next to
  `task.md` through `$TASK_DIR`, the absolute path of the task folder. Its environment goes
  through `config.scrub_env()`, so host API keys and tokens such as `GITHUB_TOKEN` are not passed on.

**Caution:** the `verify` command runs code that the agent wrote, so run `harness2-judge` only on a
machine where that is acceptable.

**bench.toml** sets the words the shared prompts use. All keys are optional:

```toml
work = "review commercial contracts for a small company's legal team"
domain_word = "contract type"
deliverable_word = "review memo"
```

`work` must describe the work, not how it is judged: the words benchmark, evaluation, grader,
rubric and score are rejected. A key that neither file knows is an error, so a typo shows up at
once.

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
- If you edit `task.md`, the `outputs` list or anything in `files/` after a run, the next run
  re-rolls that task instead of reusing its rollouts. Editing `verify` does not, because it only
  runs when you judge.
