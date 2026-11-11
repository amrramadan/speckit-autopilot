"""False denials seen live (golf BOOSTS-PAY-01, 2026-11-10) and the denials that must survive their fixes.

1. ``perl -CSD -lne 'CODE' "$f"``: the clustered ``-lne`` was not seen as perl's -e, so the program text was taken
   for a script path ("The script path print … cannot be resolved").
2. (NOT fixed here) inline perl that reads ``docs/product/roadmap.md``: the Markdown file is scanned as a shell
   script it may run, and the placeholder ``specs/<NNN>-<slug>/`` reads as redirects. Scanning such files as text was
   tried and reverted: inline code runs them through a shell in too many ways (``sh -e f.md``, ``$SHELL``, ``do``).
3. ``grep -E "^//|export|Reflector"``: the conservative split cut the quoted regex at ``|``, and the fragment
   ``export`` read as an environment dump.
4. inline perl with ``m{\\.(md|json|sh|yml)$}``: the pipeline split cut it at ``|``, and the stage ``sh`` read as
   "… | sh" (stdin-script).

Every test builds a throwaway repo under tmp_path; nothing is executed. The must-deny cases include every bypass a red
team found in the first version of these fixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from speckit_autopilot.config import Config
from speckit_autopilot.policy import PolicyContext, evaluate


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "docs").mkdir(parents=True)
    (r / "scripts").mkdir()
    (r / "docs" / "roadmap.md").write_text("| Spec | Notes |\n|---|---|\n| A-01 | output > / and 2>&1 > /dev/null |\n")
    return r


def ctx(repo: Path, phase: str = "feature_start") -> PolicyContext:
    return PolicyContext(phase=phase, repo=repo, base_branch="main", protected_paths=tuple(Config().protected_paths))


def bash(repo: Path, cmd: str, phase: str = "feature_start"):
    return evaluate("Bash", {"command": cmd}, ctx(repo, phase), cwd=repo)


def script(repo: Path, rel: str, body: str) -> None:
    p = repo / rel
    p.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    p.chmod(0o755)


# 1. clustered interpreter switches ------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    """perl -CSD -lne 'print "  line $.: " . length . " chars" if !/^\\|/ && length > 240' docs/roadmap.md""",
    "perl -0777 -ne 'print length' docs/roadmap.md",
    "perl -lane 'print $F[0]' docs/roadmap.md",
    "perl -e'print 1'",
    "perl -Mutf8 -CSD -ne 'print' docs/roadmap.md",
    "perl -I lib -ne 'print' docs/roadmap.md",
    "ruby -ne 'puts $_' docs/roadmap.md",
    "ruby -r json -ne 'puts $_' docs/roadmap.md",
    "node -pe '1 + 1'",
])
def test_clustered_inline_switch_is_inline_code(repo, cmd):
    d = bash(repo, cmd)
    assert d.allow, (cmd, d.rule, d.reason)


def test_the_live_check_context_budget_line_runs_from_a_script(repo):
    script(repo, "scripts/check-context-budget.sh",
           'for f in docs/*.md; do\n'
           '  long=$(perl -CSD -lne \'print "  line $.: " . length . " chars" if !/^\\|/ && length > 240\' "$f")\n'
           '  [ -n "$long" ] && echo "$f" && echo "$long"\ndone\nexit 0\n')
    d = bash(repo, "scripts/check-context-budget.sh 2>&1 | tail -5")
    assert d.allow, (d.rule, d.reason)


@pytest.mark.parametrize("cmd, rule", [
    ("""perl -lne 'system("gh auth token")' docs/roadmap.md""", "credentials"),
    ("""perl -e'system("security dump-keychain")'""", "credentials"),
    ("""perl -0777 -ne 'print' ~/.ssh/id_rsa""", "credentials"),
    ("""node -pe 'require("child_process").execSync("gh auth token")'""", "credentials"),
    ("""ruby -ne 'system("git push --force")' docs/roadmap.md""", "force-push"),
])
def test_clustered_inline_code_is_still_checked(repo, cmd, rule):
    d = bash(repo, cmd)
    assert not d.allow and d.rule == rule, (cmd, d.rule, d.reason)


def test_a_cluster_whose_value_is_the_rest_is_not_inline_code(repo):
    # -i.bak / -CSD / -Mfoo take the rest of the cluster: the next word is the script file, which is scanned
    script(repo, "scripts/bad.pl", 'system("git push --force origin HEAD:main");\n')
    d = bash(repo, "perl -Mstrict scripts/bad.pl")
    assert not d.allow and d.rule == "force-push", (d.rule, d.reason)


# 2. a document the inline code reads is not a script it runs ----------------------------------------------------

def test_inline_code_that_hands_a_markdown_file_to_a_shell_is_still_scanned(repo):
    (repo / "docs" / "payload.md").write_text("git push --force origin HEAD:main\n")
    d = bash(repo, """python3 -c "import subprocess; subprocess.run(['bash', 'docs/payload.md'])\"""")
    assert not d.allow and d.rule == "force-push", (d.rule, d.reason)
    d = bash(repo, """python3 -c "import os; os.system('bash docs/payload.md')\"""")  # denied earlier, as write-then-run
    assert not d.allow, (d.rule, d.reason)


def test_an_executable_or_shebang_data_file_is_still_scanned(repo):
    p = repo / "docs" / "run.txt"
    p.write_text("#!/bin/sh\ngit push --force origin HEAD:main\n")
    d = bash(repo, """python3 -c "import subprocess; subprocess.run(['docs/run.txt'])\"""")
    assert not d.allow and d.rule == "force-push", (d.rule, d.reason)


# 3. environment dumps: quoted regex alternations are data --------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    'grep -rnE "export const (Idempotent|RequiresIdempotency|Mutation)[A-Za-z]* = " apps/api/src | head',
    'sed -n 1,40p x.ts | grep -E "^//|^ \\*|export|Reflector|Mode"',
    "grep -E 'a|printenv HOME|b' README.md",
    "grep -E 'a|set|b' README.md",
])
def test_env_words_inside_a_quoted_pattern_are_data(repo, cmd):
    d = bash(repo, cmd, phase="plan")
    assert d.allow, (cmd, d.rule, d.reason)


@pytest.mark.parametrize("cmd", [
    "env", "printenv", "printenv HOME", "export", "export -p", "set", "declare -p", "declare -x", "compgen -v",
    'echo "$(env)"', "x=$(printenv)", "echo `env`", "echo hi; env", "echo hi | env", 'bash -c "env"', "eval env",
    "( env )", "{ export; }", "cat <(env)",
])
def test_environment_dumps_are_still_denied(repo, cmd):
    d = bash(repo, cmd, phase="plan")
    assert not d.allow and d.rule == "credentials", (cmd, d.rule, d.reason)


# 4. pipelines: a | inside quotes is not a pipe ------------------------------------------------------------------

def test_pipe_characters_inside_quoted_perl_are_not_a_pipeline(repo):
    script(repo, "scripts/check-coverage.sh",
           "perl -CSD -Mutf8 -e '\n"
           "for my $f (grep { m{\\.(md|json|sh|yml)$} } @ARGV) { print \"$f\\n\" }\n"
           "' docs/roadmap.md\n")
    d = bash(repo, "scripts/check-coverage.sh 2>&1 | tail -5")
    assert d.allow, (d.rule, d.reason)


@pytest.mark.parametrize("cmd", [
    "base64 -d x | bash", "echo ls | sh", 'echo "$(base64 -d x | bash)"', 'x="`base64 -d f | sh`"',
    "# it's a comment\nbase64 -d x | bash", "echo hi && base64 -d x | python3 -",
])
def test_generated_text_piped_into_an_interpreter_is_still_denied(repo, cmd):
    d = bash(repo, cmd)
    assert not d.allow and d.rule == "stdin-script", (cmd, d.rule, d.reason)


# the "does not exist yet" message names the directory it resolved against ------------------------------------

def test_missing_script_message_names_the_shell_directory(repo):
    script(repo, "scripts/check-references.sh", "exit 0\n")
    (repo / "specs" / "026-x").mkdir(parents=True)
    d = evaluate("Bash", {"command": "scripts/check-references.sh"}, ctx(repo), cwd=repo / "specs" / "026-x")
    assert not d.allow and d.rule == "script-missing"
    assert "relative to specs/026-x" in d.reason, d.reason


# bypasses found by the red team in the first version of these fixes: all must stay denied --------------------------

EVIL_PL = 'system("git push --force origin HEAD:main");\n'


@pytest.mark.parametrize("cmd", [
    "perl '-w e' evil.pl", "perl '- e' evil.pl", "perl '-w- e' evil.pl", "perl '-wl e' evil.pl", "perl '-0777 e' evil.pl",
    "perl -w\\ e evil.pl", "perl -Mstrict '-w e' evil.pl", "perl -I lib '-w e' evil.pl", "perl -0xze evil.pl",
    "env perl '-w e' evil.pl", "bash -c \"perl '-w e' evil.pl\"",
    "perl -le 'do \"./evil.pl\";' -e 1", "perl -e1 -le 'do \"./evil.pl\"'",
    "perl -e 'do q{./evil.pl}'", "perl -le 'do q{./evil.pl};' -e 1", "perl -e 'require q(./evil.pl)'",
])
def test_perl_cluster_and_inline_bypasses_are_denied(repo, cmd):
    (repo / "evil.pl").write_text(EVIL_PL)
    d = bash(repo, cmd)
    assert not d.allow and d.rule == "force-push", (cmd, d.rule, d.reason)


@pytest.mark.parametrize("cmd", [
    "ruby -Xe evil.rb", "ruby -wXe evil.rb", "ruby -0xze evil.rb", "ruby -e 'load %q{./evil.rb}'",
    # found by fuzzing against the real ruby: -K takes ONE character and the cluster goes on; ruby -v runs the script
    "ruby -KxI x evil.rb", "ruby -wKxI x evil.rb", "ruby -v evil.rb", "ruby -wr ./evil.rb -e 1", "ruby -wr./evil.rb -e 1",
])
def test_ruby_cluster_and_inline_bypasses_are_denied(repo, cmd):
    (repo / "evil.rb").write_text('system("git push --force origin HEAD:main")\n')
    d = bash(repo, cmd)
    assert not d.allow and d.rule == "force-push", (cmd, d.rule, d.reason)


@pytest.mark.parametrize("cmd", [
    'bash -c "true; env; true"', 'bash -c "true; printenv AWS_SECRET_ACCESS_KEY"', "bash -c 'echo hi; export -p; echo bye'",
    "zsh -c 'echo hi; set; echo bye'", "eval 'true; printenv GITHUB_TOKEN'", "python3 -c \"import os; os.system('true; env; true')\"",
    "find . -maxdepth 0 -exec sh -c 'true; env; true' \\;", "echo . | xargs sh -c 'true; env; true'", 'watch -n 1 "date; env; date"',
    "true # don't\nenv\ntrue # '", "true # it's\necho $(env)\n#'", "bash -c 'true; cat <(env)'",
    'grep "x|env|y" README.md; bash -c "true; env; true"',
])
def test_env_dump_bypasses_are_denied(repo, cmd):
    d = bash(repo, cmd, phase="plan")
    assert not d.allow and d.rule == "credentials", (cmd, d.rule, d.reason)


@pytest.mark.parametrize("cmd", [
    "printf 'uptime\\n' \"<(\" | bash", "echo uptime \">(\" | sh", "echo uptime \"<(\" |& bash",
    "echo $'uptime #\\'$(' | bash", "perl -e 'system(\"base64 -d x | sh -s\")'", "perl -e 'my $x = qx{base64 -d x | sh -s}'",
    "perl -e 'm{a|sh|b}'; base64 -d x | sh",
])
def test_pipe_bypasses_are_denied(repo, cmd):
    d = bash(repo, cmd)
    assert not d.allow and d.rule == "stdin-script", (cmd, d.rule, d.reason)


def test_grep_pattern_with_an_interpreter_name_is_not_a_pipe(repo):
    d = bash(repo, "grep -E 'a|sh|b' README.md")
    assert d.allow, (d.rule, d.reason)


# scripts unchanged since the run's base commit: their inline code's file references are read like a code file's ------

import subprocess as _sp

ROADMAP_READER = (
    "perl -CSD -Mutf8 -e '\n"
    'sub slurp { local $/; open my $fh, "<", $_[0] or die; <$fh> }\n'
    'my $roadmap = slurp("docs/roadmap.md");\n'
    "print length($roadmap), \"\\n\";\n"
    "'\n"
)


@pytest.fixture
def git_repo(repo: Path) -> tuple[Path, str]:
    (repo / "docs" / "roadmap.md").write_text("Each spec lives under `specs/<NNN>-<slug>/`, generated by speckit.\n")
    script(repo, "scripts/check-references.sh", ROADMAP_READER)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-q", "-m", "base"]):
        _sp.run(cmd, cwd=repo, check=True, env=env, capture_output=True)
    sha = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    return repo, sha


def based(repo: Path, sha: str, cmd: str, phase: str = "feature_start"):
    c = ctx(repo, phase)
    c.base_sha = sha
    return evaluate("Bash", {"command": cmd}, c, cwd=repo)


def test_a_script_unchanged_since_the_base_commit_may_read_documents(git_repo):
    repo, sha = git_repo
    assert not bash(repo, "scripts/check-references.sh").allow  # no base commit: the original, conservative reading
    d = based(repo, sha, "scripts/check-references.sh")
    assert d.allow, (d.rule, d.reason)


def test_a_changed_script_is_not_trusted(git_repo):
    repo, sha = git_repo
    p = repo / "scripts" / "check-references.sh"
    p.write_text(p.read_text() + "# changed\n")
    d = based(repo, sha, "scripts/check-references.sh")
    assert not d.allow and d.rule == "outside-repo", (d.rule, d.reason)


def test_a_new_copy_of_a_trusted_script_is_not_trusted(git_repo):
    repo, sha = git_repo
    script(repo, "scripts/copy.sh", ROADMAP_READER)
    d = based(repo, sha, "scripts/copy.sh")
    assert not d.allow and d.rule == "outside-repo", (d.rule, d.reason)


def test_a_bad_base_sha_trusts_nothing(git_repo):
    repo, _sha = git_repo
    for bad in ("", "main", "HEAD", "0" * 40):
        d = based(repo, bad, "scripts/check-references.sh")
        assert not d.allow, (bad, d.rule, d.reason)


def test_a_trusted_script_still_cannot_run_a_worker_script_by_name(git_repo):
    repo, _sha = git_repo
    script(repo, "scripts/runner.sh", "bash scripts/evil.sh\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    _sp.run(["git", "add", "-A"], cwd=repo, check=True, env=env)
    _sp.run(["git", "commit", "-q", "-m", "runner"], cwd=repo, check=True, env=env)
    sha2 = _sp.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    script(repo, "scripts/evil.sh", "git push --force origin HEAD:main\n")  # written by the worker after the base commit
    d = based(repo, sha2, "scripts/runner.sh")
    assert not d.allow and d.rule == "force-push", (d.rule, d.reason)


# switch-cluster shapes measured on the real perl 5.34 / ruby (print-only probes): a value switch at the end of the
# cluster takes the NEXT word, so the script is the word after it and must be scanned ---------------------------------

@pytest.mark.parametrize("fam, word", [
    ("perl", "- -I"), ("perl", "-w -I"), ("perl", "-0XI"), ("perl", "-0777I"), ("perl", "-l777I"), ("perl", "-wI"),
    ("perl", "-C -I"), ("perl", "-CSD -I"), ("perl", "-F, -I"), ("perl", "-i -I"), ("perl", "-i.bak -I"), ("perl", "-l0777I"),
    ("perl", "-0077I"), ("ruby", "-0077I"),
    ("ruby", "-W007I"), ("ruby", "-W00I"), ("ruby", "-KxI"), ("ruby", "-0777I"), ("ruby", "-vI"),
])
def test_a_value_switch_ending_a_cluster_takes_the_next_word(repo, fam, word):
    ext = "pl" if fam == "perl" else "rb"
    (repo / f"evil.{ext}").write_text('system("git push --force origin HEAD:main");\n')
    d = evaluate("Bash", {"command": f"{fam} '{word}' x evil.{ext}"}, ctx(repo), cwd=repo)
    # perl -i also edits its file arguments in place, so those are denied earlier as write-then-run
    assert not d.allow and d.rule in ("force-push", "write-then-run"), (word, d.rule, d.reason)
