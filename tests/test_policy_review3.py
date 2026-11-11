"""Shell-policy regressions from the third adversarial review (nothing is executed; every case is a throwaway repo):

1. interpreters and wrappers the script scan did not model (csh/tcsh/fish, awk -f, inline code that runs a script,
   arch / xcrun / cross-env / dotenv / script(1) / npm explore, pnpm recursive|m|multi, npm-run-all / run-s, nx);
2. write-then-run through a copy into a directory, git checkout/restore of a directory, patch, interpreter writes;
3. a script that does not exist yet, and deferred runs in the phases without the OS sandbox;
4. a code-extension file without a shebang run directly (the shell runs it);
5. node_modules/.bin shims, bare program names in package scripts, explicitly run build output;
6. scripts in the package caches the sandbox can write;
7. brace expansion and ~+ in script paths;
8. Makefile include, $(shell …) in any assignment, pattern rules;
9. git flags held in variables.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from speckit_autopilot.policy import PolicyGate, evaluate, evaluate_bash

from .test_policy_scripts import ctx, pkg, write

EVIL = "git push --force origin HEAD:main\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "src").mkdir(parents=True)
    write(r, "scripts/evil.sh", EVIL)
    write(r, "scripts/x.sh", "echo harmless\n")
    return r


def ship(repo: Path, cmd: str, **kw):
    return evaluate_bash(cmd, ctx(repo, "ship"), **kw)


def denied(d) -> bool:
    return not d.allow


# --------------------------------------------------------------------------------------
# 1. interpreters and wrappers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "tcsh scripts/evil.csh",
        "/bin/csh scripts/evil.csh",
        "fish scripts/evil.csh",
        "awk -f scripts/evil.awk",
        "gawk -f scripts/evil.awk /dev/null",
        "python3 -c \"import subprocess; subprocess.run(['bash','scripts/evil.sh'])\"",
        "node -e \"require('child_process').execSync('bash scripts/evil.sh')\"",
        "awk 'BEGIN{system(\"bash scripts/evil.sh\")}'",
        "arch -arm64 bash scripts/evil.sh",
        "arch -arch x86_64 bash scripts/evil.sh",
        "xcrun bash scripts/evil.sh",
        "xcrun --sdk macosx bash scripts/evil.sh",
        "npx cross-env FOO=1 bash scripts/evil.sh",
        "cross-env-shell FOO=1 bash scripts/evil.sh",
        "npx dotenv -c -- bash scripts/evil.sh",
        "npx env-cmd -f env.json bash scripts/evil.sh",
        "npm explore typescript -- bash $PWD/scripts/evil.sh",
        "script -q /dev/null bash scripts/evil.sh",
        "pnpm ce",
        "pnpm recursive run evil",
        "pnpm m run evil",
        "pnpm multi run evil",
        "npx run-s direct",
        "npx npm-run-all direct",
        "npx run-p 'dir*'",
        "npx nx run a:evil",
        "nx run-many -t evil",
        "nx evil a",
    ],
)
def test_unmodelled_interpreters_and_wrappers_are_scanned(repo, cmd):
    write(repo, "scripts/evil.csh", EVIL)
    write(repo, "scripts/evil.awk", 'BEGIN { system("git push --force origin HEAD:main") }\n')
    write(repo, "env.json", "{}")
    write(repo, "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    pkg(repo, "packages/a/package.json", _name="a", evil="git push --force origin HEAD:main", test="vitest")
    pkg(repo, test="vitest run", ce="cross-env NODE_ENV=test bash scripts/evil.sh", direct="git push --force origin HEAD:main")
    assert denied(ship(repo, cmd)), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "tcsh scripts/x.sh",
        "python3 -c \"print('scripts/x.sh')\"",
        "node -e \"console.log(require('fs').readFileSync('src/a.txt', 'utf8'))\"",
        "awk -F, '{print $1}' src/data.csv",
        "arch -arm64 node --version",
        "npx cross-env NODE_ENV=test vitest run",
        "pnpm recursive run lint",
        "npx run-s lint",
        "nx run a:test",
    ],
)
def test_harmless_forms_stay_allowed(repo, cmd):
    write(repo, "src/a.txt", "hi")
    write(repo, "src/data.csv", "a,b\n")
    write(repo, "pnpm-workspace.yaml", "packages:\n  - 'packages/*'\n")
    pkg(repo, "packages/a/package.json", _name="a", test="vitest run", lint="eslint .")
    pkg(repo, test="vitest run", lint="eslint .")
    d = ship(repo, cmd)
    assert d.allow, (cmd, d.reason)


# --------------------------------------------------------------------------------------
# 2. write-then-run
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "cp {payload}/x.sh scripts/ && bash scripts/x.sh",
        "cp {payload}/x.sh scripts && bash scripts/x.sh",
        "mv {payload}/x.sh scripts/ && bash scripts/x.sh",
        "ln -sf {payload}/x.sh scripts/ && bash scripts/x.sh",
        "install -m 755 {payload}/x.sh scripts/ && ./scripts/x.sh",
        "git checkout evil -- scripts && bash scripts/x.sh",
        "git restore --source=evil scripts && bash scripts/x.sh",
        "patch -p1 < {payload}/p.diff && bash scripts/x.sh",
        "python3 -c \"open('scripts/x.sh','w').write('rm -rf src')\" && bash scripts/x.sh",
        "node -e \"require('fs').writeFileSync('scripts/x.sh', 'x')\"; bash scripts/x.sh",
    ],
)
def test_write_then_run_through_dirs_patches_and_interpreters_is_denied(repo, tmp_path, cmd):
    payload = tmp_path / "payload"
    write(payload, "x.sh", EVIL)
    write(payload, "p.diff", "--- a/scripts/x.sh\n+++ b/scripts/x.sh\n")
    d = ship(repo, cmd.format(payload=payload))
    assert denied(d), cmd


# --------------------------------------------------------------------------------------
# 3. scripts that do not exist yet, deferred runs
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    ["bash scripts/later.sh", "(sleep 30; bash /tmp/autopilot-review3-later.sh) &", "until [ -f scripts/later.sh ]; do sleep 1; done; bash scripts/later.sh", "./scripts/later.sh", "source scripts/later.sh"],
)
def test_running_a_script_that_does_not_exist_yet_is_denied(repo, cmd):
    d = ship(repo, cmd)
    assert denied(d) and d.rule == "script-missing", (cmd, d)


def test_a_missing_file_inside_a_scanned_script_is_not_held_against_it(repo):
    # pnpm's .bin shims run "$basedir/node" only `if [ -x "$basedir/node" ]`: conditional, so not a denial
    write(repo, "scripts/wrap.sh", 'if [ -x "$(dirname "$0")/node" ]; then exec "$(dirname "$0")/node" --version; fi\necho ok\n')
    assert ship(repo, "bash scripts/wrap.sh").allow


@pytest.mark.parametrize("cmd", ["sleep 20; bash scripts/x.sh", "(sleep 5; bash scripts/x.sh) &", "nohup bash scripts/x.sh &", "bash scripts/x.sh & disown"])
def test_deferred_script_runs_are_denied_where_there_is_no_os_sandbox(repo, cmd):
    assert denied(ship(repo, cmd)), cmd
    assert evaluate_bash(cmd, ctx(repo, "implement")).allow  # the sandboxed phases keep their freedom


def test_a_background_script_run_is_denied_where_there_is_no_os_sandbox(repo):
    d = evaluate("Bash", {"command": "bash scripts/x.sh", "run_in_background": True}, ctx(repo, "ship"))
    assert denied(d) and d.rule == "deferred-script"
    assert evaluate("Bash", {"command": "bash scripts/x.sh", "run_in_background": True}, ctx(repo, "implement")).allow
    assert evaluate("Bash", {"command": "pnpm --version", "run_in_background": True}, ctx(repo, "ship")).allow


def test_a_script_that_ran_in_an_unsandboxed_phase_cannot_be_rewritten_there(repo):
    gate = PolicyGate(ctx(repo, "ship"))
    assert gate.check("Bash", {"command": "bash scripts/x.sh"}, "hook").allow
    d = gate.check("Write", {"file_path": str(repo / "scripts" / "x.sh"), "content": EVIL}, "hook")
    assert denied(d) and d.rule == "ran-script"
    assert denied(gate.check("Bash", {"command": "cp /tmp/y.sh scripts/x.sh"}, "hook"))
    assert gate.check("Write", {"file_path": str(repo / "src" / "a.py"), "content": "x"}, "hook").allow
    gate.set_phase("linear")
    assert gate.check("Write", {"file_path": str(repo / "scripts" / "x.sh"), "content": "echo"}, "hook").allow  # a new phase


# --------------------------------------------------------------------------------------
# 4. no shebang, code extension, run directly
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content, direct_allowed, node_allowed",
    [
        ("//; git push --force origin HEAD:main\n", False, True),  # the shell runs the rest of the line
        ("/*\ngit push --force origin HEAD:main\n*/\n", False, True),  # "/*" and "*/" fail as globs, the middle runs
        ("# git push --force origin HEAD:main\nconsole.log(1)\n", True, False),  # a shell comment, not a JS one
    ],
)
def test_a_code_file_without_a_shebang_run_directly_is_scanned_as_the_shell_runs_it(repo, content, direct_allowed, node_allowed):
    write(repo, "scripts/n.js", content, 0o755)
    assert ship(repo, "./scripts/n.js").allow is direct_allowed
    assert ship(repo, "node scripts/n.js").allow is node_allowed


# --------------------------------------------------------------------------------------
# 5. node_modules/.bin, bare names, build output
# --------------------------------------------------------------------------------------


def test_a_planted_bin_shim_is_scanned_directly_and_through_a_package_script(repo):
    write(repo, "node_modules/.bin/vitest", "#!/bin/sh\n" + EVIL, 0o755)
    pkg(repo, test="vitest run")
    assert denied(ship(repo, "./node_modules/.bin/vitest"))
    assert denied(ship(repo, "pnpm test"))
    assert denied(ship(repo, "npx vitest run"))


def test_a_real_pnpm_shim_passes(repo):
    shim = (
        '#!/bin/sh\nbasedir=$(dirname "$(echo "$0" | sed -e \'s,\\\\,/,g\')")\n\ncase `uname` in\n    *CYGWIN*) basedir=`cygpath -w "$basedir"`;;\nesac\n\n'
        'if [ -z "$NODE_PATH" ]; then\n  export NODE_PATH="/x/node_modules/.pnpm/node_modules"\nelse\n  export NODE_PATH="/x/node_modules/.pnpm/node_modules:$NODE_PATH"\nfi\n'
        'if [ -x "$basedir/node" ]; then\n  exec "$basedir/node"  "$basedir/../vitest/vitest.mjs" "$@"\nelse\n  exec node  "$basedir/../vitest/vitest.mjs" "$@"\nfi\n'
    )
    write(repo, "node_modules/.bin/vitest", shim, 0o755)
    write(repo, "node_modules/vitest/vitest.mjs", "import './dist/cli.js'\n")
    pkg(repo, test="vitest run")
    d = ship(repo, "pnpm test")
    assert d.allow, d.reason


def test_a_symlinked_bin_entry_into_a_package_is_not_read(repo):
    write(repo, "node_modules/vitest/vitest.mjs", "// huge vendored code that mentions sudo and git reset --hard\n")
    (repo / "node_modules" / ".bin").mkdir(parents=True)
    os.symlink("../vitest/vitest.mjs", repo / "node_modules" / ".bin" / "vitest")
    pkg(repo, test="vitest run")
    assert ship(repo, "pnpm test").allow


def test_the_worker_cannot_write_into_installed_packages(repo):
    c = ctx(repo, "implement")
    for rel in ("node_modules/.bin/vitest", "node_modules/vitest/index.js", ".venv/bin/pytest"):
        d = evaluate("Write", {"file_path": str(repo / rel), "content": "x"}, c)
        assert denied(d) and d.rule == "installed-package", rel
    assert denied(evaluate_bash("cp /tmp/x node_modules/.bin/vitest", c))
    assert evaluate_bash("rm -rf node_modules", c).allow


def test_explicitly_run_build_output_is_scanned(repo):
    write(repo, "dist/x.js", "require('child_process').execSync('git push --force origin HEAD:main')\n")
    assert denied(ship(repo, "node dist/x.js"))
    write(repo, "build/ok.js", "console.log('ok')\n")
    assert ship(repo, "node build/ok.js").allow


# --------------------------------------------------------------------------------------
# 6. package caches
# --------------------------------------------------------------------------------------


def test_scripts_in_the_sandbox_writable_package_caches_are_scanned(repo, tmp_path, monkeypatch):
    cache = tmp_path / "npm-cache"
    write(cache, "x.sh", EVIL)
    monkeypatch.setenv("npm_config_cache", str(cache))
    assert denied(ship(repo, f"bash {cache}/x.sh"))
    assert denied(ship(repo, f"cd {cache} && bash x.sh"))


# --------------------------------------------------------------------------------------
# 7. brace expansion and ~+
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", ["bash scripts/{evil,nothing}.sh", "bash scripts/{x,evil}.sh", "bash ~+/scripts/evil.sh", "bash scripts/ev{i,o}l.sh"])
def test_shell_expanded_script_paths_resolve_like_the_shell(repo, cmd):
    assert denied(ship(repo, cmd)), cmd


def test_tilde_minus_cannot_be_resolved_and_is_denied(repo):
    d = ship(repo, "bash ~-/scripts/evil.sh")
    assert denied(d) and d.rule == "script-path"


# --------------------------------------------------------------------------------------
# 8. Makefiles
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "makefile, extra, cmd",
    [
        ("include tools/extra.mk\nall:\n\techo hi\n", {"tools/extra.mk": "test:\n\tgit push --force origin HEAD:main\n"}, "make test"),
        ("-include tools/*.mk\nall:\n\techo hi\n", {"tools/extra.mk": "test:\n\tbash scripts/evil.sh\n"}, "make test"),
        ("X := $(shell bash scripts/evil.sh)\nall:\n\techo hi\n", {}, "make all"),
        ("X = $(shell git push --force origin HEAD:main)\nall:\n\techo $(X)\n", {}, "make all"),
        ("all: $(shell bash scripts/evil.sh)\n\techo hi\n", {}, "make all"),
        ("%:\n\t@bash scripts/evil.sh\nall:\n\techo hi\n", {}, "make foo"),
        ("%.o: %.c\n\tbash scripts/evil.sh\n", {}, "make src/a.o"),
        (".DEFAULT:\n\tbash scripts/evil.sh\n", {}, "make anything"),
    ],
)
def test_makefile_includes_shell_functions_and_pattern_rules_are_scanned(repo, makefile, extra, cmd):
    write(repo, "Makefile", makefile)
    for rel, text in extra.items():
        write(repo, rel, text)
    assert denied(ship(repo, cmd)), cmd


def test_a_plain_makefile_still_passes(repo):
    write(repo, "Makefile", "include tools/vars.mk\nCC := $(shell which cc)\n%.o: %.c\n\t$(CC) -c $< -o $@\nall: src/a.o\n\techo done\n")
    write(repo, "tools/vars.mk", "NAME = demo\n")
    assert ship(repo, "make all").allow


# --------------------------------------------------------------------------------------
# 9. git flags in variables
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd, phase",
    [
        ("H=--hard; git reset $H HEAD~1", "implement"),
        ("F=-fdx; git clean $F", "implement"),
        ("F=--force; git push $F origin HEAD:main", "ship"),
        ("export F=--force && git push \"$F\" origin feature/x", "ship"),
        ("F=$(echo --force); git push $F origin feature/x", "ship"),
        ("git push $(printf -- --force) origin feature/x", "ship"),
        ("git push $FLAGS origin feature/x", "ship"),
        ("D=-D; git branch $D main", "ship"),
    ],
)
def test_git_flags_held_in_variables_are_checked(repo, cmd, phase):
    assert denied(evaluate_bash(cmd, ctx(repo, phase))), cmd


def test_a_script_that_hides_a_flag_in_a_variable_is_denied(repo):
    write(repo, "scripts/var.sh", "F=--force\ngit push $F origin HEAD:main\n")
    assert denied(ship(repo, "bash scripts/var.sh"))


@pytest.mark.parametrize(
    "cmd",
    [
        "git push -u origin $(git branch --show-current)",
        "BRANCH=$(git rev-parse --abbrev-ref HEAD); git push -u origin \"$BRANCH\"",
        "git push -u origin \"$GIT_BRANCH_NAME\"",
        "B=feature/x; git push -u origin $B",
        "git checkout -q \"$BRANCH_NAME\"",
        "git branch --list \"$BRANCH_NAME\"",
        "MSG='feat: x'; git commit -m \"$MSG\"",
    ],
)
def test_ordinary_variables_in_git_commands_stay_allowed(repo, cmd):
    d = ship(repo, cmd)
    assert d.allow, (cmd, d.reason)


@pytest.mark.parametrize(
    "cmd, path, content",
    [
        ("osascript scripts/evil.scpt", "scripts/evil.scpt", 'do shell script "git push --force origin HEAD:main"\n'),
        ("osascript -e 'do shell script \"bash scripts/evil.sh\"'", None, None),
        ("expect -f scripts/evil.exp", "scripts/evil.exp", "spawn git push --force origin HEAD:main\n"),
        ("tclsh scripts/evil.tcl", "scripts/evil.tcl", "exec git push --force origin HEAD:main\n"),
        ("npx concurrently 'pnpm direct' 'echo hi'", None, None),
        ("npx concurrently npm:direct", None, None),
    ],
)
def test_other_command_running_interpreters_and_concurrently_are_scanned(repo, cmd, path, content):
    if path:
        write(repo, path, content)
    pkg(repo, direct="git push --force origin HEAD:main", ok="echo ok")
    assert denied(ship(repo, cmd)), cmd
    assert ship(repo, "npx concurrently 'pnpm ok' 'echo hi'").allow
