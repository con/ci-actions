"""Tests for annex_check.py, on throwaway git-annex repositories"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE.parent / "annex_check.py"
sys.path.insert(0, str(HERE.parent))

import annex_check  # noqa: E402

# the policy of con/talks
GITATTRIBUTES = """\
* annex.backend=MD5E
**/.git* annex.largefiles=nothing
* annex.largefiles=((mimeencoding=binary)and(largerthan=0))
*.svg annex.largefiles=(largerthan=100K)
"""


@pytest.fixture(autouse=True)
def git_env(monkeypatch):
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Tester")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "tester@example.com")
    for var in ("GITHUB_ACTIONS", "GITHUB_EVENT_PATH", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(var, raising=False)


def git(repo, *args, **kw):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, **kw
    ).stdout.strip()


def binary(path, size, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes((i * 7 + seed) % 256 for i in range(size)))


def commit_to_git(repo, *paths, message="commit to git"):
    """Commit files to git, whatever annex.largefiles says"""
    git(repo, "-c", "annex.largefiles=nothing", "add", "--", *paths)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def annex(repo, *paths, message="annex"):
    git(repo, "annex", "add", "--quiet", "--", *paths)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def check(repo, *args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    git(tmp_path, "init", "-q", "-b", "master", str(repo))
    git(repo, "annex", "init", "-q", "upstream")
    (repo / ".gitattributes").write_text(GITATTRIBUTES)
    (repo / "README.md").write_text("readme\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def work(upstream, tmp_path):
    """A contributor's clone, on branch pr"""
    repo = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(upstream), str(repo))
    git(repo, "annex", "init", "-q", "contributor laptop")
    git(repo, "checkout", "-q", "-b", "pr")
    return repo


def errors(result):
    return [ln for ln in result.stdout.splitlines() if ln.startswith(("ERROR", "::error"))]


#
# largefiles
#


def test_largefiles_ok(work):
    binary(work / "pics" / "fig.png", 3000)
    annex(work, "pics/fig.png")
    (work / "notes.md").write_text("text\n")
    (work / "small.svg").write_text("<svg/>\n")
    binary(work / ".hidden.bin", 100)  # dotfiles go to git by default
    commit_to_git(work, "notes.md", "small.svg", ".hidden.bin")
    r = check(work, "--base", "origin/master", "--checks", "largefiles")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "All of them are fine in git" in r.stdout


def test_largefiles_violations_in_every_commit(work):
    binary(work / "pics" / "fig.png", 3000)
    (work / "notes.md").write_text("text\n")
    first = commit_to_git(work, "pics/fig.png", "notes.md")
    binary(work / "pics" / "fig.png", 4000, seed=1)
    second = commit_to_git(work, "pics/fig.png")
    (work / "big.svg").write_text("<svg>" + "x" * 200_000 + "</svg>\n")
    binary(work / "gone.pdf", 1000)
    third = commit_to_git(work, "big.svg", "gone.pdf")
    git(work, "rm", "-q", "gone.pdf")
    git(work, "commit", "-q", "-m", "rm")

    r = check(work, "--base", "origin/master", "--checks", "largefiles")
    assert r.returncode == 1
    errs = errors(r)
    assert len(errs) == 4, r.stdout
    out = "\n".join(errs)
    assert f"pics/fig.png (commit {first[:8]})" in out
    assert f"pics/fig.png (commit {second[:8]})" in out
    assert f"big.svg (commit {third[:8]})" in out
    assert "(largerthan=100K) (.gitattributes)" in out
    assert f"gone.pdf (commit {third[:8]})" in out
    assert "notes.md" not in out
    assert "Hint: These commits can be rewritten" in r.stdout


def test_largefiles_odd_names(work):
    names = ["with space.png", "-dash.png", "юникод.png"]
    # git-annex (10.20260901) adds this one to git: libmagic gets its name
    # with U+FFFD for the \xff, so mimeencoding=binary does not match
    not_utf8 = os.fsdecode(b"bad\xff.png")
    for i, name in enumerate(names + [not_utf8]):
        binary(work / name, 500, seed=i)
    commit_to_git(work, *names, not_utf8)
    r = check(work, "--base", "origin/master", "--checks", "largefiles")
    assert r.returncode == 1
    out = "\n".join(errors(r))
    assert all(name in out for name in names), r.stdout
    assert "Could not tell" not in out


def test_largefiles_dotfiles_option(work):
    binary(work / ".hidden.bin", 100)
    commit_to_git(work, ".hidden.bin")
    assert check(work, "--base", "origin/master", "--checks", "largefiles").returncode == 0
    r = check(work, "--base", "origin/master", "--checks", "largefiles", "--dotfiles", "true")
    assert r.returncode == 1
    assert ".hidden.bin" in "\n".join(errors(r))


def test_largefiles_default_policy_annexes_everything(tmp_path):
    """Without annex.largefiles configured, git-annex annexes all non-dotfiles"""
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", "-b", "master", str(repo))
    git(repo, "annex", "init", "-q")
    (repo / ".gitignore").write_text("*.tmp\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-q", "-m", "init")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "README.md").write_text("text\n")
    commit_to_git(repo, "README.md")
    r = check(repo, "--base", base, "--checks", "largefiles")
    assert r.returncode == 1
    assert "annex.largefiles is not configured" in r.stdout


def test_largefiles_git_annex_config(work, upstream):
    """`git annex config` applies where .gitattributes do not say"""
    (upstream / ".gitattributes").write_text("* annex.backend=MD5E\n")
    git(upstream, "commit", "-q", "-am", "no largefiles in .gitattributes")
    git(upstream, "annex", "config", "--set", "annex.largefiles", "include=*.dat")
    git(work, "pull", "-q", "origin", "master")
    git(work, "fetch", "-q", "origin", "git-annex")
    git(work, "annex", "merge")
    (work / "a.dat").write_text("data\n")
    (work / "a.txt").write_text("text\n")
    commit_to_git(work, "a.dat", "a.txt")
    r = check(work, "--base", "origin/master", "--checks", "largefiles")
    assert r.returncode == 1
    out = "\n".join(errors(r))
    assert "a.dat" in out and "include=*.dat (git annex config)" in out
    assert "a.txt" not in out


def test_largefiles_ignores_base_and_merged_base(work, upstream):
    # in git on the base branch already: not this branch's doing
    binary(upstream / "old.png", 500)
    commit_to_git(upstream, "old.png")
    git(work, "fetch", "-q", "origin")
    git(work, "merge", "-q", "--no-edit", "origin/master")
    (work / "notes.md").write_text("text\n")
    commit_to_git(work, "notes.md")
    r = check(work, "--base", "origin/master", "--checks", "largefiles")
    assert r.returncode == 0, r.stdout


#
# fix
#


def test_fix(work):
    binary(work / "pics" / "fig.png", 3000)
    (work / "notes.md").write_text("text\n")
    commit_to_git(work, "pics/fig.png", "notes.md", message="first")
    binary(work / "pics" / "fig.png", 4000, seed=1)  # modified in git again
    commit_to_git(work, "pics/fig.png", message="second")
    binary(work / "top.jpg", 1000)
    commit_to_git(work, "top.jpg", message="third")
    before = git(work, "log", "--format=%an %ae %ad %s", "origin/master..")

    r = check(work, "fix", "--base", "origin/master")
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(work, "log", "--format=%an %ae %ad %s", "origin/master..") == before
    assert git(work, "status", "--porcelain") == ""
    for path in ("pics/fig.png", "top.jpg"):
        assert (work / path).is_symlink()
        git(work, "annex", "fsck", "-q", "--", path)
    assert git(work, "annex", "find", "--format=${bytesize}\\n", "pics/fig.png") == "4000"
    first = git(work, "rev-list", "-1", "--grep=first", "HEAD")
    assert git(work, "ls-tree", first, "pics/fig.png").startswith("120000")
    assert git(work, "ls-tree", first, "notes.md").startswith("100644")
    # the first version is annexed too
    key = os.path.basename(git(work, "cat-file", "blob", f"{first}:pics/fig.png"))
    assert key.startswith("MD5E-s3000--")
    git(work, "annex", "fsck", "-q", "--key", key)

    r = check(work, "--base", "origin/master", "--checks", "largefiles")
    assert r.returncode == 0, r.stdout


def test_fix_keeps_merges(work, upstream):
    binary(work / "fig.png", 3000)
    commit_to_git(work, "fig.png")
    (upstream / "other.md").write_text("text\n")
    git(upstream, "add", "other.md")
    git(upstream, "commit", "-q", "-m", "upstream change")
    git(work, "fetch", "-q", "origin")
    git(work, "merge", "-q", "--no-edit", "origin/master")
    r = check(work, "fix", "--base", "origin/master")
    assert r.returncode == 0, r.stdout + r.stderr
    parents = git(work, "rev-list", "--parents", "-1", "HEAD").split()[1:]
    assert len(parents) == 2
    assert git(work, "rev-parse", "origin/master") in parents
    assert (work / "fig.png").is_symlink()


def test_fix_nothing_to_do(work):
    (work / "notes.md").write_text("text\n")
    commit_to_git(work, "notes.md")
    head = git(work, "rev-parse", "HEAD")
    r = check(work, "fix", "--base", "origin/master")
    assert r.returncode == 0
    assert "Nothing to fix" in r.stdout
    assert git(work, "rev-parse", "HEAD") == head


def test_fix_refuses_dirty_tree(work):
    binary(work / "fig.png", 3000)
    commit_to_git(work, "fig.png")
    (work / "README.md").write_text("changed\n")
    r = check(work, "fix", "--base", "origin/master")
    assert r.returncode == 2
    assert "uncommitted changes" in r.stderr


#
# availability
#


@pytest.fixture
def store(upstream, tmp_path):
    """Another clone, where content can be put"""
    repo = tmp_path / "store"
    git(tmp_path, "clone", "-q", str(upstream), str(repo))
    git(repo, "annex", "init", "-q", "content store")
    return repo


def test_availability(work, store):
    binary(work / "fig.png", 3000)
    annex(work, "fig.png")
    r = check(work, "--base", "origin/master", "--checks", "availability")
    assert r.returncode == 1
    out = "\n".join(errors(r))
    assert "fig.png" in out
    assert "contributor laptop" in out and "(this clone)" in out

    git(work, "remote", "add", "store", str(store))
    git(work, "annex", "copy", "-q", "--to=store", "fig.png")
    r = check(work, "--base", "origin/master", "--checks", "availability")
    assert r.returncode == 0, r.stdout
    assert "store: has 1 of 1" in r.stdout


def test_availability_extra_remote_and_earlier_versions(work, store, upstream, tmp_path):
    binary(work / "fig.png", 3000)
    annex(work, "fig.png", message="v1")
    git(work, "annex", "unlock", "fig.png")
    binary(work / "fig.png", 4000, seed=1)
    annex(work, "fig.png", message="v2")
    git(work, "remote", "add", "store", str(store))
    git(work, "annex", "copy", "-q", "--to=store", "fig.png")  # only v2
    git(work, "annex", "sync", "-q", "--only-annex", "--no-content", "store")

    # "CI": a fresh clone of upstream, with the pull request's commits
    ci = tmp_path / "ci"
    git(tmp_path, "clone", "-q", str(upstream), str(ci))
    git(ci, "fetch", "-q", str(work), "pr")
    head = git(ci, "rev-parse", "FETCH_HEAD")
    r = check(ci, "--base", "origin/master", "--head", head, "--checks", "availability")
    assert r.returncode == 1  # v2 is nowhere to be found
    r = check(ci, "--base", "origin/master", "--head", head, "--checks", "availability",
              "--remote", f"mystore={store}")
    assert r.returncode == 0, r.stdout
    assert "mystore: has 1 of 2" in r.stdout
    # v1 is reported, but does not fail the check
    warnings = [ln for ln in r.stdout.splitlines() if ln.startswith("WARNING")]
    assert len(warnings) == 1 and "later commits replace or remove" in warnings[0]
    assert "contributor laptop" in warnings[0]  # from store's git-annex branch
    # remotes added for the check are removed after a local run
    assert "mystore" not in git(ci, "remote").split()


def test_availability_web(work, tmp_path):
    binary(work / "fig.png", 3000)
    annex(work, "fig.png")
    key = git(work, "annex", "lookupkey", "fig.png")
    # a URL git-annex cannot get it from does not count
    git(work, "annex", "registerurl", key, "http://127.0.0.1:9/fig.png")
    git(work, "annex", "drop", "-q", "--force", "fig.png")
    r = check(work, "--base", "origin/master", "--checks", "availability")
    assert r.returncode == 1


#
# CI: event payload
#


def test_pull_request_event(work, upstream, store, tmp_path):
    binary(work / "fig.png", 3000)
    annex(work, "fig.png")
    binary(work / "raw.png", 300)
    commit_to_git(work, "raw.png")
    git(work, "remote", "add", "store", str(store))
    git(work, "annex", "copy", "-q", "--to=store", "fig.png")
    git(work, "annex", "sync", "-q", "--only-annex", "--no-content", "store")

    ci = tmp_path / "ci"
    git(tmp_path, "clone", "-q", str(upstream), str(ci))
    git(ci, "fetch", "-q", str(work), "pr")
    head = git(ci, "rev-parse", "FETCH_HEAD")
    event = {
        "pull_request": {
            "number": 1,
            "author_association": "CONTRIBUTOR",
            "user": {"login": "someone"},
            "body": "Adds a figure.\n\nExtra git-annex remote: https://example.org/someone/repo\n",
            "base": {"ref": "master", "sha": git(ci, "rev-parse", "origin/master"),
                     "repo": {"full_name": "o/r", "clone_url": str(upstream)}},
            "head": {"ref": "pr", "sha": head,
                     # the fork: content is there too, but this check should find it on store
                     "repo": {"full_name": "someone/r", "clone_url": str(store)}},
        },
        "repository": {"full_name": "o/r", "owner": {"login": "o"}},
    }
    (tmp_path / "event.json").write_text(json.dumps(event))
    summary = tmp_path / "summary.md"
    env = dict(os.environ, GITHUB_ACTIONS="true", GITHUB_EVENT_PATH=str(tmp_path / "event.json"),
               GITHUB_STEP_SUMMARY=str(summary))
    r = check(ci, env=env)
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert any(ln.startswith("::error file=raw.png,title=File committed to git") for ln in lines)
    # not trusted to name remotes
    assert any(ln.startswith("::notice::Not using extra git-annex remote https://example.org")
               for ln in lines)
    assert "annex-check-pr-head: has 1 of 1" in r.stdout
    assert f"git fetch {upstream} master" in r.stdout
    assert "raw.png" in summary.read_text()


#
# units
#


@pytest.mark.parametrize(
    "body,urls",
    [
        ("Extra git-annex remote: https://a.org/x/y", ["https://a.org/x/y"]),
        ("text\n- extra git-annex remote:  <https://a.org/x>  \nmore", ["https://a.org/x"]),
        ("> Extra git-annex remote: https://a.org/1\r\nExtra git-annex remote: https://b.org/2",
         ["https://a.org/1", "https://b.org/2"]),
        ("see Extra git-annex remote: https://a.org/x", []),  # not at line start
        ("Extra git-annex remote: https://a.org/x and more", []),
        ("", []),
    ],
)
def test_pr_body_remote_urls(body, urls):
    assert annex_check.pr_body_remote_urls(body) == urls


@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://hub.example.org/me/repo", True),
        ("http://hub.example.org/me/repo", False),
        ("https://user:pw@hub.example.org/me/repo", False),
        ("ext::sh -c touch% /tmp/x", False),
        ("file:///etc", False),
        ("/local/path", False),
    ],
)
def test_valid_pr_body_url(url, ok):
    assert annex_check.valid_pr_body_url(url) is ok


@pytest.mark.parametrize(
    "pr,trusted",
    [
        ({"author_association": "OWNER"}, True),
        ({"author_association": "MEMBER"}, True),
        ({"author_association": "COLLABORATOR"}, True),
        ({"author_association": "CONTRIBUTOR"}, False),
        ({"author_association": "FIRST_TIME_CONTRIBUTOR"}, False),
        ({"user": {"login": "o"}}, True),  # Forgejo: repository owner
        ({"user": {"login": "x"}}, False),  # Forgejo: unknown without API
    ],
)
def test_pr_author_trusted(pr, trusted, monkeypatch):
    monkeypatch.delenv("ANNEX_CHECK_TOKEN", raising=False)
    event = {"repository": {"full_name": "o/r", "owner": {"login": "o"}}}
    assert annex_check.pr_author_trusted(event, pr)[0] is trusted


@pytest.mark.parametrize(
    "data,pointer,key",
    [
        (b"../../.git/annex/objects/Xx/Yy/MD5E-s3--abc.png/MD5E-s3--abc.png", False,
         "MD5E-s3--abc.png"),
        (b".git/annex/objects/Xx/Yy/SHA256E-s0--e3b0.txt/SHA256E-s0--e3b0.txt", False,
         "SHA256E-s0--e3b0.txt"),
        (b"/annex/objects/MD5E-s3--abc.png\n", True, "MD5E-s3--abc.png"),
        (b"../elsewhere/file", False, None),
        (b"/annex/objects/not a key", True, None),
        (b"plain content", True, None),
    ],
)
def test_annex_key(data, pointer, key):
    assert annex_check.annex_key(data, pointer) == key


def test_worktree_groups():
    C = annex_check.Change
    changes = [C("c1", "a", "100644", "1"), C("c2", "a", "100644", "2"),
               C("c3", "a/b", "100644", "3"), C("c4", "x/y", "100644", "4")]
    groups = annex_check._worktree_groups(changes)
    for group in groups:
        paths = [c.path for c in group]
        assert len(set(paths)) == len(paths)
        assert not ("a" in paths and "a/b" in paths)
    assert sorted(c.blob for g in groups for c in g) == ["1", "2", "3", "4"]
