#!/usr/bin/env python3
"""Check that commits follow the git-annex policies of their repository.

Commits are those reachable from HEAD but not from BASE (e.g. those of a
pull request).

`check` runs these checks:

largefiles
    No file was committed directly to git which git-annex (`git annex add`,
    `datalad save`) would have annexed according to the repository's
    annex.largefiles configuration: .gitattributes, `git annex config`, or
    git-annex's default of annexing everything but dotfiles.  Every commit
    is checked, not only the final state: once merged, a blob committed to
    git stays in the history for good.

availability
    The content of every file these commits annex can be obtained from some
    remote reachable from here, not only from the clone of whoever committed
    it.  Remotes are those configured in the repository, those given with
    --remote, the head repository of a pull request (e.g. a fork), and those
    named in the description of a pull request with lines like

        Extra git-annex remote: https://hub.example.org/me/repo

    if its author may add them (see --pr-body-remotes).  Their git-annex
    branches are fetched for location information.

`fix` rewrites the commits so that the files the largefiles check flags are
annexed instead.

Runs in GitHub or Forgejo Actions, where it reads the pull request or push
from the event payload, as well as locally.  Needs git and git-annex.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass

CHECKS = ("largefiles", "availability")

# Authors of pull requests whose "Extra git-annex remote:" lines are followed
# by default (GitHub's author_association)
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

PR_BODY_REMOTE_RE = re.compile(
    r"^[ \t>*+-]*extra[ \t]+git-annex[ \t]+remote[ \t]*:[ \t]*<?([^\s<>]+)>?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Name prefix of the remotes this script adds
REMOTE_PREFIX = "annex-check-"

# git-annex keys, e.g. MD5E-s1234--d41d8cd98f00b204e9800998ecf8427e.png
KEY_RE = re.compile(r"^[A-Z0-9]+(?:-[a-zA-Z][0-9]+)*--[^/]*$")

# Unlocked annexed files are committed as pointer files no larger than this
POINTER_MAX_SIZE = 32 * 1024

REGULAR_MODES = {"100644", "100755"}
SYMLINK_MODE = "120000"

# How long network operations (fetching, checking a remote) may take, in seconds
NETWORK_TIMEOUT = 900

DEFAULT_SCRIPT_URL = (
    "https://raw.githubusercontent.com/con/ci-actions/HEAD/annex-check/annex_check.py"
)

CI = os.environ.get("GITHUB_ACTIONS") == "true"


class Failure(Exception):
    """An error that ends the run with a message, but no traceback"""


def run(cmd, *, cwd=None, input=None, env=None, check=True, text=True, timeout=None):
    """Run a command, return its CompletedProcess; raise Failure if it fails"""
    kwargs = {"encoding": "utf-8", "errors": "surrogateescape"} if text else {}
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            input=input,
            env=env,
            capture_output=True,
            timeout=timeout,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        raise Failure(f"`{shlex.join(cmd)}` timed out after {timeout} seconds")
    except FileNotFoundError:
        raise Failure(f"{cmd[0]} is not installed")
    if check and proc.returncode:
        stderr = proc.stderr if text else proc.stderr.decode(errors="replace")
        raise Failure(f"`{shlex.join(cmd)}` failed: {stderr.strip()}")
    return proc


def git(*args, **kwargs) -> str:
    """Run git, return its stripped output"""
    return run(["git", *args], **kwargs).stdout.strip()


def git_z(*args, **kwargs) -> list[str]:
    """Run git with NUL-separated output, return its records"""
    out = run(["git", *args], **kwargs).stdout
    return out.split("\0")[:-1] if out else []


def git_ok(*args, **kwargs) -> bool:
    """Run git, return whether it succeeded"""
    return run(["git", *args], check=False, **kwargs).returncode == 0


def as_json_text(path: str) -> str:
    """path as git-annex renders it in JSON"""
    return path.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


def write_blob(blob: str, dest: str):
    with open(dest, "wb") as f:
        subprocess.run(["git", "cat-file", "blob", blob], stdout=f, check=True)


def commit_exists(rev: str) -> bool:
    return git_ok("rev-parse", "--quiet", "--verify", f"{rev}^{{commit}}")


def short(sha: str) -> str:
    return sha[:8]


def human_size(size: int) -> str:
    for unit in ("bytes", "kB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000


def rmtree(path):
    """Remove a directory tree, including git-annex's write-protected objects"""

    def make_writable(func, p, _exc):
        for d in (os.path.dirname(p), p):
            if os.path.exists(d) and not os.path.islink(d):
                os.chmod(d, os.stat(d).st_mode | stat.S_IWUSR | stat.S_IXUSR)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


#
# Reporting
#


def _escape_data(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(s: str) -> str:
    return _escape_data(s).replace(":", "%3A").replace(",", "%2C")


class Report:
    """Messages to the log (as CI annotations in CI) and the job summary"""

    def __init__(self):
        self.failed = False
        self.summary: list[str] = []

    def _annotate(self, level: str, message: str, title=None, file=None):
        if CI:
            props = ",".join(
                f"{k}={_escape_property(v)}"
                for k, v in (("file", file), ("title", title))
                if v
            )
            print(f"::{level}{' ' + props if props else ''}::{_escape_data(message)}")
        else:
            print(f"{level.upper()}: {message}")
        sys.stdout.flush()

    def error(self, message: str, **kw):
        self.failed = True
        self._annotate("error", message, **kw)

    def warning(self, message: str, **kw):
        self._annotate("warning", message, **kw)

    def notice(self, message: str, **kw):
        self._annotate("notice", message, **kw)

    @staticmethod
    def info(message: str = ""):
        print(message)
        sys.stdout.flush()

    def section(self, title: str):
        self.info(f"\n=== {title} ===")
        self.summary.append(f"### {title}\n")

    def hint(self, text: str):
        lines = text.strip("\n").splitlines()
        self.info("\n".join(["Hint: " + lines[0]] + ["      " + ln if ln else "" for ln in lines[1:]]))

    def write_summary(self):
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path and self.summary:
            with open(path, "a", encoding="utf-8") as f:
                f.write("## git-annex checks\n\n" + "\n".join(self.summary) + "\n")


#
# What the commits add
#


@dataclass
class Change:
    """A blob that a commit puts at a path, and which is new to the repository"""

    commit: str
    path: str
    mode: str
    blob: str
    size: int = 0
    # annex key, if the blob is an annexed file (symlink or pointer file)
    key: str | None = None

    @property
    def location(self) -> str:
        return f"{self.path} (commit {short(self.commit)})"


def annex_key(data: bytes, pointer: bool) -> str | None:
    """The key that a symlink target or pointer file refers to, if any"""
    text = data.decode("utf-8", errors="surrogateescape")
    if pointer:
        if not text.startswith("/annex/objects/"):
            return None
        text = text.split("\n", 1)[0]
    elif "annex/objects/" not in text:
        return None
    key = text.rsplit("/", 1)[-1]
    return key if KEY_RE.match(key) else None


def blob_sizes(shas: list[str]) -> dict[str, int]:
    if not shas:
        return {}
    out = git("cat-file", "--batch-check", input="".join(s + "\n" for s in shas))
    return {sha: int(size) for sha, _type, size in (ln.split() for ln in out.splitlines())}


def read_blobs(shas: list[str]) -> dict[str, bytes]:
    if not shas:
        return {}
    out = run(
        ["git", "cat-file", "--batch"],
        input="".join(s + "\n" for s in shas).encode(),
        text=False,
    ).stdout
    blobs, pos = {}, 0
    for _ in shas:
        eol = out.index(b"\n", pos)
        sha, _type, size = out[pos:eol].decode().split()
        start = eol + 1
        blobs[sha] = out[start : start + int(size)]
        pos = start + int(size) + 1
    return blobs


def collect_changes(head: str, bases: list[str]) -> tuple[list[str], list[Change]]:
    """The commits in head but not in bases, and the new blobs they add

    A blob counts once per path: where the commit (oldest first) that first
    puts it there.  Blobs that bases already have (e.g. files merged in from
    the base branch) are not new.
    """
    excluded = ["--not", *bases] if bases else []
    commits = git("rev-list", "--reverse", "--topo-order", head, *excluded).split()
    new_objects = set(
        git("rev-list", "--objects", "--no-object-names", head, *excluded).split()
    )
    changes, seen = [], set()
    for commit in commits:
        # -m: a merge commit is compared with each of its parents; what comes
        # from the merged branches is not new or was seen in its own commit
        fields = git_z(
            "diff-tree", "-r", "-m", "--root", "--no-renames", "--no-abbrev",
            "--no-commit-id", "-z", commit,
        )
        for meta, path in zip(fields[::2], fields[1::2]):
            _src_mode, mode, _src, blob, status = meta.lstrip(":").split()
            if status == "D" or blob not in new_objects or (path, blob) in seen:
                continue
            seen.add((path, blob))
            changes.append(Change(commit, path, mode, blob))

    shas = sorted({c.blob for c in changes if c.mode in REGULAR_MODES | {SYMLINK_MODE}})
    sizes = blob_sizes(shas)
    contents = read_blobs([s for s in shas if sizes[s] <= POINTER_MAX_SIZE])
    for c in changes:
        c.size = sizes.get(c.blob, 0)
        if c.blob in contents:
            c.key = annex_key(contents[c.blob], pointer=c.mode != SYMLINK_MODE)
    return commits, changes


def files_in_git(changes: list[Change]) -> list[Change]:
    """Changes that put files (not annexed, no symlinks) into git"""
    return [
        c
        for c in changes
        if c.mode in REGULAR_MODES
        and c.key is None
        # must be in git for git to work, whatever annex.largefiles says
        and not os.path.basename(c.path).startswith(".git")
    ]


#
# largefiles check
#


@dataclass
class Verdict:
    """What `git annex add` does with a file"""

    key: str | None  # annexed with this key, or added to git
    note: str = ""
    error: str = ""


def _worktree_groups(changes: list[Change]) -> list[list[Change]]:
    """Group changes so that the paths in each group can coexist in a work tree"""
    groups: list[tuple[list[Change], set, set]] = []
    for c in changes:
        parents = {"/".join(c.path.split("/")[:i]) for i in range(1, c.path.count("/") + 1)}
        for members, paths, dirs in groups:
            if c.path not in paths and c.path not in dirs and not parents & paths:
                break
        else:
            members, paths, dirs = [], set(), set()
            groups.append((members, paths, dirs))
        members.append(c)
        paths.add(c.path)
        dirs.update(parents)
    return [members for members, _, _ in groups]


def annex_add_verdicts(
    head: str,
    files: list[Change],
    *,
    largefiles: str | None = None,
    dotfiles: bool | None = None,
) -> tuple[dict[tuple[str, str], Verdict], dict[str, str]]:
    """What `git annex add` would have done with the files

    git-annex itself decides, so that its full annex.largefiles syntax (and
    the precedence of where it is configured) applies.  It runs in a scratch
    clone with the .gitattributes files of head (which also determine the
    key backend), the repository's `git annex config`, and the files' content.

    Returns the verdict for each (path, blob), and for each annexed path the
    rule that made git-annex annex it.
    """
    top = git("rev-parse", "--show-toplevel")
    tmp = tempfile.mkdtemp(prefix="annex-check-")
    try:
        scratch = os.path.join(tmp, "repo")
        git("clone", "--quiet", "--shared", "--no-checkout", top, scratch)

        def sgit(*args, **kw):
            return git(*args, cwd=scratch, **kw)

        # no need to enable special remotes in the scratch clone
        if not git_ok("annex", "init", "--quiet", "--no-autoenable", cwd=scratch):
            sgit("annex", "init", "--quiet")
        if largefiles is not None:
            sgit("config", "annex.largefiles", largefiles)
        if dotfiles is not None:
            sgit("config", "annex.dotfiles", str(dotfiles).lower())

        # {path: blob}; git ignores .gitattributes that are symlinks
        attributes = {}
        for entry in git_z("ls-tree", "-r", "-z", head):
            meta, path = entry.split("\t", 1)
            mode, _type, blob = meta.split()
            if os.path.basename(path) == ".gitattributes" and mode in REGULAR_MODES:
                attributes[path] = blob

        def fresh_worktree(paths):
            """An empty index, and a work tree with only head's .gitattributes

            Leaves out those that could not coexist with files at paths: the
            commits that have such a file have no directory there.
            """
            for entry in os.listdir(scratch):
                if entry != ".git":
                    dest = os.path.join(scratch, entry)
                    if os.path.isdir(dest) and not os.path.islink(dest):
                        rmtree(dest)
                    else:
                        os.unlink(dest)
            sgit("read-tree", "--empty")
            prefixes = tuple(p + "/" for p in paths)
            for p, blob in attributes.items():
                if not p.startswith(prefixes):
                    dest = os.path.join(scratch, p)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    write_blob(blob, dest)

        verdicts = {}
        for group in _worktree_groups(files):
            fresh_worktree([c.path for c in group])
            for c in group:
                dest = os.path.join(scratch, c.path)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                write_blob(c.blob, dest)
            # git-annex's JSON has U+FFFD for bytes that are not UTF-8
            by_path = {as_json_text(c.path): c for c in group}
            out = run(
                ["git", "annex", "add", "--json", "--json-error-messages",
                 "--no-check-gitignore", "--batch", "-z"],
                cwd=scratch, input="".join(c.path + "\0" for c in group), check=False,
            ).stdout
            for line in out.splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                c = by_path.get(rec.get("file"))
                if c is None:
                    continue
                verdicts[(c.path, c.blob)] = Verdict(
                    rec.get("key"),
                    note=rec.get("note", ""),
                    error="; ".join(rec.get("error-messages") or []),
                )

        annexed = sorted({p for (p, _), v in verdicts.items() if v.key})
        fresh_worktree([])
        rules = _largefiles_rules(scratch, annexed, largefiles) if annexed else {}
        return verdicts, rules
    finally:
        rmtree(tmp)


def _largefiles_rules(scratch: str, paths: list[str], override: str | None) -> dict[str, str]:
    """Describe the annex.largefiles setting that applies to each path"""
    if override is not None:
        return {p: f"annex.largefiles={override} (given to annex-check)" for p in paths}
    fields = git_z("check-attr", "-z", "--stdin", "annex.largefiles", cwd=scratch,
                   input="\0".join(paths) + "\0")
    attrs = {p: v for p, _attr, v in zip(fields[::3], fields[1::3], fields[2::3])}
    annex_config = git("annex", "config", "--get", "annex.largefiles", cwd=scratch, check=False)
    rules = {}
    for p in paths:
        value = attrs.get(p, "unspecified")
        if value not in ("unspecified", "unset"):
            rules[p] = f"annex.largefiles={value} (.gitattributes)"
        elif annex_config:
            rules[p] = f"annex.largefiles={annex_config} (git annex config)"
        else:
            rules[p] = "annex.largefiles is not configured, so git-annex annexes all non-dotfiles"
    return rules


def find_violations(head, files, args) -> tuple[list[tuple[Change, Verdict]], dict[str, str], list[tuple[Change, Verdict]]]:
    """Files that are in git but would have been annexed, and those that could not be evaluated"""
    verdicts, rules = annex_add_verdicts(
        head, files, largefiles=args.largefiles, dotfiles=args.dotfiles
    )
    violations, unknown = [], []
    for c in files:
        v = verdicts.get((c.path, c.blob))
        if v is None or (v.key is None and v.error):
            unknown.append((c, v or Verdict(None, error="git annex add reported nothing")))
        elif v.key:
            violations.append((c, v))
    return violations, rules, unknown


def check_largefiles(ctx: Context, changes: list[Change], report: Report):
    report.section("largefiles: nothing is in git that git-annex would annex")
    files = files_in_git(changes)
    if not files:
        report.info("No files were committed directly to git.")
        report.summary.append("No files were committed directly to git.\n")
        return
    report.info(f"Checking {len(files)} file(s) committed directly to git ...")
    if "MagicMime" not in git("annex", "version"):
        report.warning("This git-annex is built without MagicMime: mimetype= and mimeencoding= "
                       "in annex.largefiles match no file", title="annex-check: largefiles")
    violations, rules, unknown = find_violations(ctx.head, files, ctx.args)
    for c, v in unknown:
        report.error(f"Could not tell whether git-annex would annex {c.location}: {v.error}",
                     title="annex-check: largefiles", file=c.path)
    if not violations:
        report.info("All of them are fine in git.")
        report.summary.append(f"All {len(files)} file(s) committed directly to git belong there.\n")
        return

    report.summary.append(
        f"**{len(violations)} file(s) were committed to git, but belong in git-annex:**\n"
    )
    for c, v in violations:
        report.error(
            f"{c.location}, {human_size(c.size)}, was committed to git, "
            f"but git-annex would annex it: {rules.get(c.path, '')}",
            title="File committed to git instead of git-annex",
            file=c.path,
        )
        report.summary.append(
            f"- `{c.path}` ({human_size(c.size)}) in {short(c.commit)}: {rules.get(c.path, '')}"
        )
    report.summary.append("")
    hint = fix_hint(ctx)
    report.hint(hint)
    report.summary.append("<details><summary>How to fix</summary>\n\n" + hint + "\n</details>\n")


def fix_hint(ctx: Context) -> str:
    script = os.environ.get("ANNEX_CHECK_SCRIPT_URL") or DEFAULT_SCRIPT_URL
    fix = f"curl -fsSL {script} | python3 - fix"
    if ctx.base_fetch:
        url, ref = ctx.base_fetch
        commands = f"git fetch {shlex.quote(url)} {shlex.quote(ref)}\n    {fix} --base FETCH_HEAD"
    else:
        bases = " ".join(f"--base {shlex.quote(b)}" for b in ctx.bases) or "--base BASE"
        commands = f"{fix} {bases}"
    return f"""\
These commits can be rewritten so that git-annex holds these files instead.
With git-annex installed, in your clone with this branch checked out, run

    {commands}

then make the newly annexed content available (e.g. `git annex copy --to=REMOTE`
or `datalad push --to=REMOTE`), and `git push --force-with-lease`.
Or by hand: `git rebase -i` the commits listed above with `edit`, and for each
file in them run `git rm --cached FILE && git annex add FILE`, then
`git commit --amend --no-edit` and `git rebase --continue`.
"""


#
# availability check
#


def remote_urls() -> dict[str, str]:
    """{url: name} of the configured remotes"""
    out = git("config", "--get-regexp", r"^remote\..*\.url$", check=False)
    urls = {}
    for line in out.splitlines():
        key, _, url = line.partition(" ")
        urls.setdefault(url, key[len("remote."):-len(".url")])
    return urls


def annex_remotes() -> list[str]:
    """Names of the remotes git-annex may be able to get content from"""
    out = git("config", "--get-regexp", r"^remote\..*\.(url|annex-uuid)$", check=False)
    names = []
    for line in out.splitlines():
        key = line.split(" ", 1)[0]
        name = key[len("remote."):key.rindex(".")]
        if name not in names:
            names.append(name)
    return [
        n for n in names
        if git("config", "--type=bool", f"remote.{n}.annex-ignore", check=False) != "true"
    ]


def add_remotes(specs: list[tuple[str | None, str, str]], report: Report) -> tuple[list[str], list[str]]:
    """Add remotes (unless configured already) and fetch their git-annex branches

    Returns the names of the remotes, and of those among them that were added.
    """
    existing = remote_urls()
    taken = set(git("remote").split())
    names, added = [], []
    for name, url, why in specs:
        if url in existing:
            name = existing[url]
        else:
            if name is None or name in taken:
                n = 1
                while f"{REMOTE_PREFIX}{n}" in taken:
                    n += 1
                name = f"{REMOTE_PREFIX}{n}"
            git("remote", "add", name, url)
            taken.add(name)
            added.append(name)
            existing[url] = name
        if name in names:
            continue
        names.append(name)
        report.info(f"Remote {name}: {url} ({why})")
        # `git annex sync` pushes to synced/git-annex of non-bare repositories
        branches = [
            line.split()[1][len("refs/heads/"):]
            for line in run(
                ["git", "ls-remote", name, "refs/heads/git-annex", "refs/heads/synced/git-annex"],
                check=False, timeout=NETWORK_TIMEOUT,
            ).stdout.splitlines()
        ]
        if not branches:
            report.info("  has no git-annex branch (or cannot be reached)")
            continue
        try:
            git("fetch", "--quiet", "--no-tags", name,
                *(f"+refs/heads/{b}:refs/remotes/{name}/{b}" for b in branches),
                timeout=NETWORK_TIMEOUT)
        except Failure as e:
            report.warning(f"Could not fetch the git-annex branch of {url}: {e}")
    return names, added


def whereis_descriptions(key: str) -> list[str]:
    """Descriptions of the repositories the location log says have the key"""
    proc = run(["git", "annex", "whereis", "--json", "--key", key], check=False)
    try:
        rec = json.loads(proc.stdout.splitlines()[0])
    except (IndexError, ValueError):
        return []
    return [
        (w.get("description") or w.get("uuid", "?")) + (" (this clone)" if w.get("here") else "")
        for w in rec.get("whereis", [])
    ]


def check_availability(ctx: Context, changes: list[Change], report: Report):
    report.section("availability: annexed content can be obtained from a reachable remote")
    by_key: dict[str, list[Change]] = defaultdict(list)
    for c in changes:
        if c.key:
            by_key[c.key].append(c)
    if not by_key:
        report.info("No files were annexed.")
        report.summary.append("No files were annexed.\n")
        return

    # keys of annexed files that are in head (rather than only in earlier commits)
    paths = sorted({c.path for cs in by_key.values() for c in cs})
    in_head = set()
    for entry in git_z("ls-tree", "-r", "-z", "--full-tree", ctx.head, "--", *paths):
        meta, path = entry.split("\t", 1)
        in_head.add((path, meta.split()[2]))
    final_keys = {k for k, cs in by_key.items() if any((c.path, c.blob) in in_head for c in cs)}

    specs = ctx.remote_specs
    names, added = add_remotes(specs, report) if specs else ([], [])
    ctx.added_remotes = added
    # the given remotes first, then the rest
    remotes = names + [r for r in annex_remotes() if r not in names] + ["web"]

    report.info(f"Looking for {len(by_key)} key(s) on: {', '.join(remotes)}")
    pending = sorted(by_key)
    found: dict[str, str] = {}
    unusable = []
    for remote in remotes:
        if not pending:
            break
        proc = run(
            ["git", "annex", "checkpresentkey", "--batch", remote],
            input="".join(k + "\n" for k in pending),
            check=False,
            timeout=NETWORK_TIMEOUT,
        )
        if proc.returncode:
            unusable.append(remote)
            report.info(f"  {remote}: cannot be checked: {proc.stderr.strip()}")
            continue
        results = proc.stdout.splitlines()
        present = {k for k, r in zip(pending, results) if r.strip() == "1"}
        report.info(f"  {remote}: has {len(present)} of {len(pending)}")
        for k in present:
            found[k] = remote
        pending = [k for k in pending if k not in present]

    checked = [r for r in remotes if r not in unusable]
    if not pending:
        report.info("All annexed content is available.")
        report.summary.append(
            f"The content of all {len(by_key)} annexed key(s) is available "
            f"(from {', '.join(sorted(set(found.values())))}).\n"
        )
        return

    missing = [k for k in pending if k in final_keys]
    earlier = [k for k in pending if k not in final_keys]
    if missing:
        report.summary.append(
            f"**Content of {len(missing)} annexed file(s) is not available from "
            f"any of {', '.join(checked)}:**\n"
        )
    for k in missing + earlier:
        cs = by_key[k]
        claimed = whereis_descriptions(k)
        where = (
            "git-annex knows of copies only in: " + "; ".join(claimed)
            if claimed
            else "git-annex knows of no copy (was the git-annex branch pushed?)"
        )
        files = ", ".join(c.location for c in cs)
        if k in final_keys:
            report.error(
                f"Content of {files} is not available from any of "
                f"{', '.join(checked)} (key {k}). {where}",
                title="Annexed content not available",
                file=cs[0].path,
            )
            report.summary.append(f"- `{cs[0].path}` (`{k}`): {where}")
        else:
            report.warning(
                f"Content of {files}, which later commits replace or remove, is not "
                f"available from any of {', '.join(checked)} (key {k}). {where}",
                title="Annexed content not available (earlier version)",
                file=cs[0].path,
            )
    hint = availability_hint(ctx, checked)
    report.hint(hint)
    report.summary.append("\n<details><summary>How to fix</summary>\n\n" + hint + "\n</details>\n")


def availability_hint(ctx: Context, remotes: list[str]) -> str:
    urls = {name: url for url, name in remote_urls().items()}
    listed = "\n".join(
        f"    {r}: {urls[r]}" if r in urls else f"    {r}" for r in remotes if r != "web"
    )
    if listed:
        to = f"""\
Make the content available from one of the remotes checked here, e.g. with
`git annex copy --to=REMOTE FILES` (or `datalad push --to=REMOTE`):

{listed}
"""
    else:
        to = """\
None of the remotes of this repository can hold annexed content; name some
in the annex-remotes input of the action (--remote), and copy the content
there, e.g. with `git annex copy --to=REMOTE FILES`,"""
    return f"""\
{to}
or register a URL for it (`git annex addurl`, `git annex registerurl`), and push
the git-annex branch.  If the content is already on a remote not listed here,
e.g. your fork on a forge that supports git-annex, say so in the pull request
description, with a line like

    Extra git-annex remote: https://example.org/you/repo

(followed for pull requests of repository owners, members, and collaborators).
"""


#
# Context: which commits, which remotes
#


@dataclass
class Context:
    args: argparse.Namespace
    head: str
    bases: list[str]
    # what to `git fetch` to get the base (URL, ref), for hints
    base_fetch: tuple[str, str] | None = None
    # remotes to look for content on: (name or None, url, why)
    remote_specs: list = None
    added_remotes: list = None


def load_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_remote_spec(spec: str) -> tuple[str | None, str]:
    """NAME=URL or URL"""
    name, sep, url = spec.partition("=")
    if sep and REMOTE_NAME_RE.match(name):
        return name, url
    return None, spec


def valid_pr_body_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not any(ch.isspace() or ord(ch) < 32 for ch in url)
    )


def pr_body_remote_urls(body: str) -> list[str]:
    # GitHub has CRLF line endings in pull request descriptions
    return PR_BODY_REMOTE_RE.findall((body or "").replace("\r\n", "\n"))


def pr_author_trusted(event: dict, pr: dict) -> tuple[bool, str]:
    """Whether the pull request's author may name remotes, and why"""
    assoc = pr.get("author_association")
    login = (pr.get("user") or {}).get("login") or ""
    if assoc:
        return assoc in TRUSTED_ASSOCIATIONS, f"author association is {assoc}"
    # Forgejo/Gitea payloads lack author_association
    repo = event.get("repository") or {}
    if login and login == (repo.get("owner") or {}).get("login"):
        return True, "the author owns the repository"
    api, token = os.environ.get("GITHUB_API_URL"), os.environ.get("ANNEX_CHECK_TOKEN")
    if api and token and login and repo.get("full_name"):
        url = f"{api}/repos/{repo['full_name']}/collaborators/{urllib.parse.quote(login)}"
        req = urllib.request.Request(url, headers={"Authorization": f"token {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status in (200, 204):
                    return True, f"{login} is a collaborator"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, f"{login} is not a collaborator"
        except urllib.error.URLError:
            pass
    return False, f"could not determine whether {login or 'the author'} is a collaborator"


def make_context(args, event: dict, report: Report) -> Context:
    pr = event.get("pull_request") or {}
    bases, head, base_fetch = list(args.base), args.head, None
    if pr:
        base = pr["base"]
        if not bases:
            bases = [base["sha"]]
            if commit_exists(f"refs/remotes/origin/{base['ref']}"):
                bases.append(f"refs/remotes/origin/{base['ref']}")
        head = head or pr["head"]["sha"]
        clone_url = (base.get("repo") or {}).get("clone_url")
        if clone_url:
            base_fetch = (clone_url, base["ref"])
    elif event.get("after") and not head:  # push
        head = event["after"]
        if not bases:
            before = event.get("before") or ""
            default = (event.get("repository") or {}).get("default_branch")
            if before.strip("0"):
                bases = [before]
            elif default and event.get("ref") != f"refs/heads/{default}":
                bases = [f"refs/remotes/origin/{default}"]
    head = head or "HEAD"
    if not bases and not event:
        if commit_exists("refs/remotes/origin/HEAD"):
            bases = ["refs/remotes/origin/HEAD"]
        else:
            raise Failure("No base to compare with: specify --base")

    for rev in [head, *bases]:
        if not commit_exists(rev) and event:
            # e.g. a shallow or single-branch checkout
            git_ok("fetch", "--quiet", "--no-tags", "origin", rev, timeout=NETWORK_TIMEOUT)
        if not commit_exists(rev):
            raise Failure(f"Commit {rev} is not available here; fetch it first")
    head = git("rev-parse", f"{head}^{{commit}}")

    specs = []
    for spec in args.remote:
        name, url = parse_remote_spec(spec)
        specs.append((name, url, "given to annex-check"))
    if pr:
        head_repo, base_repo = pr["head"].get("repo") or {}, pr["base"].get("repo") or {}
        if head_repo.get("clone_url") and head_repo.get("full_name") != base_repo.get("full_name"):
            specs.append((f"{REMOTE_PREFIX}pr-head", head_repo["clone_url"],
                          "head repository of the pull request"))
        urls = pr_body_remote_urls(pr.get("body") or "")
        if urls:
            if args.pr_body_remotes == "none":
                trusted, why = False, "--pr-body-remotes is none"
            elif args.pr_body_remotes == "anyone":
                trusted, why = True, "--pr-body-remotes is anyone"
            else:
                trusted, why = pr_author_trusted(event, pr)
            for url in urls:
                if not trusted:
                    report.notice(f"Not using extra git-annex remote {url} from the pull "
                                  f"request description: {why}")
                elif not valid_pr_body_url(url):
                    report.warning(f"Not using extra git-annex remote {url} from the pull "
                                   "request description: only https:// URLs are supported")
                else:
                    specs.append((None, url, "named in the pull request description"))
    return Context(args=args, head=head, bases=bases, base_fetch=base_fetch,
                   remote_specs=specs, added_remotes=[])


def prepare_repository(event: bool, report: Report):
    """Make sure the repository is a git-annex repository with full history"""
    if git("rev-parse", "--is-shallow-repository") == "true":
        report.info("Fetching the full history of this shallow clone ...")
        git("fetch", "--quiet", "--unshallow", "--no-tags", "origin", timeout=NETWORK_TIMEOUT)
    if event:
        # actions/checkout fetches only branches it needs, depending on settings
        git_ok("fetch", "--quiet", "--no-tags", "origin",
               "+refs/heads/git-annex:refs/remotes/origin/git-annex", timeout=NETWORK_TIMEOUT)
        if not git("config", "user.email", check=False):
            # git-annex records its state in commits
            for var, value in (("NAME", "annex-check"), ("EMAIL", "annex-check@localhost")):
                os.environ.setdefault(f"GIT_AUTHOR_{var}", value)
                os.environ.setdefault(f"GIT_COMMITTER_{var}", value)
    if not git("config", "annex.uuid", check=False):
        report.info("Initializing git-annex ...")
        git("annex", "init", "--quiet", timeout=NETWORK_TIMEOUT)


def cmd_check(args) -> int:
    report = Report()
    checks = [c for c in re.split(r"[\s,]+", args.checks) if c]
    unknown = set(checks) - set(CHECKS)
    if unknown:
        raise Failure(f"Unknown checks: {', '.join(sorted(unknown))}; known: {', '.join(CHECKS)}")
    event = load_event()
    prepare_repository(bool(event), report)
    ctx = make_context(args, event, report)
    commits, changes = collect_changes(ctx.head, ctx.bases)
    report.info(
        f"Checking {len(commits)} commit(s) in {short(ctx.head)} but not in "
        f"{', '.join(ctx.bases) or '(nothing)'}"
    )
    try:
        # largefiles first: availability merges git-annex branches from
        # remotes, which could carry other `git annex config`
        if "largefiles" in checks:
            check_largefiles(ctx, changes, report)
        if "availability" in checks:
            check_availability(ctx, changes, report)
    finally:
        if not event:
            for name in ctx.added_remotes or []:
                git("remote", "remove", name, check=False)
        report.write_summary()
    report.info("\nFAILED" if report.failed else "\nOK")
    return 1 if report.failed else 0


#
# fix
#


def link_target(path: str, object_path: str) -> str:
    """Target of the symlink at path to an annexed object (.git/annex/objects/...)"""
    return "../" * path.count("/") + object_path


def commit_like(commit: str, tree: str, parents: list[str]) -> str:
    """A commit with the given tree and parents, and the message and author of commit"""
    raw = run(["git", "cat-file", "commit", commit], text=False).stdout
    header, _, message = raw.partition(b"\n\n")
    env, config = dict(os.environ), []
    for line in header.decode("utf-8", errors="surrogateescape").split("\n"):
        field, _, value = line.partition(" ")
        if field == "author":
            m = re.match(r"(.*) <(.*)> (\d+ [+-]\d{4})$", value)
            if m:
                env.update(GIT_AUTHOR_NAME=m[1], GIT_AUTHOR_EMAIL=m[2], GIT_AUTHOR_DATE="@" + m[3])
        elif field == "encoding":
            config = ["-c", f"i18n.commitEncoding={value}"]
    parent_args = [arg for p in parents for arg in ("-p", p)]
    return run(
        ["git", *config, "commit-tree", tree, *parent_args], input=message, text=False, env=env
    ).stdout.decode().strip()


def cmd_fix(args) -> int:
    report = Report()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise Failure("The work tree has uncommitted changes; commit or stash them first")
    if not git_ok("symbolic-ref", "--quiet", "HEAD"):
        raise Failure("Not on a branch; check out the branch to rewrite first")
    if not args.base:
        raise Failure("Specify --base: the commit(s) that should not be rewritten")
    for b in args.base:
        if not commit_exists(b):
            raise Failure(f"Commit {b} is not available here; fetch it first")
    prepare_repository(False, report)
    head = git("rev-parse", "HEAD")
    _, changes = collect_changes(head, args.base)
    files = files_in_git(changes)
    violations, _rules, unknown = find_violations(head, files, args) if files else ([], {}, [])
    for c, v in unknown:
        report.error(f"Could not tell whether git-annex would annex {c.location} (left as is): "
                     f"{v.error}")
    if not violations:
        report.info("Nothing to fix: no committed file belongs in git-annex.")
        return 1 if report.failed else 0

    # put the content into the annex
    keys = {v.key: c.blob for c, v in violations}
    tmp = tempfile.mkdtemp(prefix="annex-check-fix-")
    try:
        for key, blob in keys.items():
            if git_ok("annex", "contentlocation", key):
                continue
            write_blob(blob, os.path.join(tmp, key))
            git("annex", "reinject", "--guesskeys", os.path.join(tmp, key))
    finally:
        rmtree(tmp)
    object_paths = dict(
        zip(keys, git("annex", "examinekey", "--batch", "--format=${objectpath}\n",
                      input="".join(k + "\n" for k in keys)).splitlines())
    )

    targets = {(c.path, c.blob): link_target(c.path, object_paths[v.key]) for c, v in violations}
    paths = sorted({p for p, _ in targets})
    rewritten: dict[str, str] = {}
    index = tempfile.mkstemp(prefix="annex-check-index-")
    os.close(index[0])
    index_env = dict(os.environ, GIT_INDEX_FILE=index[1])
    try:
        for line in git("rev-list", "--reverse", "--topo-order", "--parents", head,
                        "--not", *args.base).splitlines():
            commit, *parents = line.split()
            new_parents = [rewritten.get(p, p) for p in parents]
            tree = git("rev-parse", f"{commit}^{{tree}}")
            updates = []
            for entry in git_z("ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths):
                meta, path = entry.split("\t", 1)
                target = targets.get((path, meta.split()[2]))
                if target:
                    link = git("hash-object", "-w", "--stdin", input=target)
                    updates.append(f"{SYMLINK_MODE} {link}\t{path}")
            new_tree = tree
            if updates:
                git("read-tree", commit, env=index_env)
                git("update-index", "-z", "--index-info", input="\0".join(updates) + "\0",
                    env=index_env)
                new_tree = git("write-tree", env=index_env)
            if new_tree != tree or new_parents != parents:
                rewritten[commit] = commit_like(commit, new_tree, new_parents)
    finally:
        os.unlink(index[1])

    git("reset", "--quiet", "--keep", rewritten.get(head, head))
    report.info(f"Rewrote {len(rewritten)} commit(s), annexing:")
    for c, v in violations:
        report.info(f"  {c.path} (was in {short(c.commit)})")
    files = " ".join(shlex.quote(p) for p in paths)
    report.info(f"""
The previous state is in ORIG_HEAD (`git reset --keep ORIG_HEAD` goes back to it).
Next, make the content available, e.g. `git annex copy --to=REMOTE -- {files}`
(or `datalad push --to=REMOTE`), and `git push --force-with-lease`.""")
    return 1 if report.failed else 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("check", "fix", "-h", "--help"):
        argv.insert(0, "check")

    def split_words(values):
        return [w for v in values for w in v.split()]

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--base", action="append", default=[], metavar="REV",
        help="commits in this are not checked (repeatable; by default the base of the "
        "pull request or the previous tip of the pushed branch, or locally origin/HEAD)",
    )
    common.add_argument(
        "--largefiles", metavar="EXPR",
        help="use this annex.largefiles expression instead of the repository's",
    )
    common.add_argument(
        "--dotfiles", choices=("true", "false"),
        help="set annex.dotfiles (true: dotfiles are subject to annex.largefiles, as with "
        "`datalad save`; default: the repository's `git annex config`, else false)",
    )
    check = sub.add_parser("check", parents=[common], help="check commits (default)")
    check.add_argument("--head", metavar="REV",
                       help="tip of the commits to check (default: pull request head or HEAD)")
    check.add_argument(
        "--checks", default=",".join(CHECKS),
        help=f"comma-separated checks to run (default: {','.join(CHECKS)})",
    )
    check.add_argument(
        "--remote", action="append", default=[], metavar="[NAME=]URL",
        help="also look for annexed content on this remote, added unless configured "
        "already (repeatable; whitespace-separated lists are fine)",
    )
    check.add_argument(
        "--pr-body-remotes", choices=("collaborators", "anyone", "none"),
        default="collaborators",
        help="whose 'Extra git-annex remote: URL' lines in the pull request description "
        "to follow (default: collaborators, i.e. owners, members, and collaborators)",
    )
    sub.add_parser("fix", parents=[common],
                   help="rewrite the commits (on the current branch) to annex the "
                   "files the largefiles check flags")
    args = parser.parse_args(argv)
    args.base = split_words(args.base)
    args.dotfiles = None if args.dotfiles is None else args.dotfiles == "true"
    if args.command == "check":
        args.remote = split_words(args.remote)

    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    os.environ["GIT_LITERAL_PATHSPECS"] = "1"
    try:
        git("rev-parse", "--git-dir")
        if not git_ok("annex", "version", "--raw"):
            raise Failure("git-annex is not installed")
        return cmd_check(args) if args.command == "check" else cmd_fix(args)
    except Failure as e:
        print(f"{'::error::' if CI else 'ERROR: '}{e}", file=sys.stdout if CI else sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
