# annex-check: git-annex policy checks for pull requests

A GitHub (and, hopefully, Forgejo) Action, and a standalone script
([`annex_check.py`](annex_check.py)), checking the commits of a pull request
(or a push) in a [git-annex](https://git-annex.branchable.com/) repository:

- **largefiles**: no file was committed directly to git which git-annex
  (`git annex add`, `datalad save`) would have annexed per the repository's
  [`annex.largefiles`](https://git-annex.branchable.com/tips/largefiles/)
  configuration — `.gitattributes`, `git annex config`, or git-annex's default
  of annexing everything but dotfiles.  git-annex itself decides, by adding
  the files' content in a scratch clone, so the full `annex.largefiles`
  syntax (`mimeencoding=`, `largerthan=`, ...) applies.  Every commit is
  checked, not only the final state: once merged, a blob committed to git
  stays in the history for good.  Failures come with a hint on how to rewrite
  the commits (see [Fixing](#fixing-files-committed-to-git)).
- **availability**: the content of every file the commits annex can be
  obtained from some remote reachable from CI — not just from the clone of
  whoever committed it.  Each key is verified with
  `git annex checkpresentkey` on the remotes; the location log alone is not
  trusted.  Remotes are
  - those of the CI clone (`origin`, special remotes with `autoenable=true`,
    and URLs registered for the keys),
  - those given in the `annex-remotes` input,
  - the head repository of the pull request, if it is a fork, and
  - remotes named in the pull request description with lines like

        Extra git-annex remote: https://hub.example.org/me/repo

    if its author is an owner, member, or collaborator of the repository
    (see `pr-body-remotes`).

  The git-annex branches of all of these are fetched for location
  information, which is shown for content that is not available.  Content
  that only earlier commits of the pull request refer to (replaced or removed
  later) is reported as a warning, not an error.

## Usage

```yaml
name: git-annex

on:
  pull_request:
    # edited: to follow changes to "Extra git-annex remote:" lines
    types: [opened, synchronize, reopened, edited]

permissions:
  contents: read

jobs:
  annex-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0  # all commits of the pull request, and the git-annex branch
      - uses: con/ci-actions/annex-check@master
        with:
          annex-remotes: |
            https://datasets.datalad.org/centerforopenneuroscience/talks/.git
            https://hub.centerforopenneuroscience.org/con/talks
```

The action installs git-annex from [PyPI](https://pypi.org/project/git-annex/)
with [uv](https://docs.astral.sh/uv/), and needs Python 3.8+ and git 2.26+.
It does not run any code from the pull request.

### Inputs

| Input | Default | Description |
| --- | --- | --- |
| `checks` | `largefiles,availability` | Checks to run, comma-separated. |
| `annex-remotes` | | Remotes to also look for annexed content on, one per line, as `URL` or `NAME=URL`. |
| `pr-body-remotes` | `collaborators` | Whose `Extra git-annex remote: URL` lines in the pull request description to follow: `collaborators` (owners, members, collaborators), `anyone`, or `none`. Only `https://` URLs are followed. |
| `base` | base of the pull request, or previous tip of the pushed branch | Commits in these revisions are not checked. |
| `head` | head of the pull request, or the pushed commit | Tip of the commits to check. |
| `largefiles` | | An `annex.largefiles` expression to use instead of the repository's configuration. |
| `dotfiles` | the repository's `git annex config annex.dotfiles`, else `false` | `true` to subject dotfiles to `annex.largefiles` too, as `datalad save` does. |
| `git-annex-version` | latest | Version of git-annex to install from PyPI, or `system` to use the installed one. |
| `token` | `github.token` | To ask the API whether the author is a collaborator, where the event does not say (Forgejo). |

## Fixing files committed to git

`annex_check.py fix --base BASE` rewrites the commits of the current branch
that are not in `BASE` so that the files the largefiles check flags are
annexed (locked) instead, in every commit that has them — without the
conflicts that a `git rebase` would run into when later commits modify such
a file.  Commit messages, authors, and dates are kept; merges stay merges.
The content goes into the local annex, to be copied to a remote before
force-pushing.  The CI log prints the exact commands, along the lines of

```sh
git fetch https://github.com/con/talks master
curl -fsSL https://raw.githubusercontent.com/con/ci-actions/master/annex-check/annex_check.py | python3 - fix --base FETCH_HEAD
git annex copy --to=REMOTE ...   # or: datalad push --to=REMOTE
git push --force-with-lease
```

`git reset --keep ORIG_HEAD` undoes the rewrite.

## Running locally

```sh
annex_check.py check --base origin/master            # both checks, on HEAD
annex_check.py check --base origin/master --checks largefiles
annex_check.py check --base origin/master --remote https://hub.example.org/me/repo
```

`--base` defaults to `origin/HEAD`.  Remotes given with `--remote` that are
not configured already are removed again afterwards.  See `annex_check.py
--help`.

The policy is the one shared with the repository (`.gitattributes` of the
checked commit, `git annex config`), not a local `git config
annex.largefiles`.

## Forgejo

The script reads the event payload the same way on Forgejo Actions, where
pull request payloads lack `author_association`: the author may name remotes
in the description if they own the repository, or if the API (with the
`token` input) says they are a collaborator.  The action should work there
as `uses: https://github.com/con/ci-actions/annex-check@master` or from a
mirror, on a runner image with bash, curl, git, and python3.  Untested so far.

## Tests

```sh
python3 -m pytest annex-check/tests
```

They need git and git-annex, and create throwaway repositories.
[`.github/workflows/annex-check.yml`](../.github/workflows/annex-check.yml)
runs them with the latest git-annex from PyPI and with Ubuntu's.
