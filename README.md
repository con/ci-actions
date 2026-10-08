# Fixture for testing the annex-check action

This branch (`test-annex-check/base`) and the `test-annex-check/*` branches
based on it are not part of the actions: they are a tiny git-annex
"repository" whose pull requests exercise
[`annex-check`](https://github.com/con/ci-actions/tree/claude/bold-bohr-lvtger/annex-check)
on real pull request events.  Do not merge them.

`.gitattributes` is the policy of [con/talks](https://github.com/con/talks).
Annexed content is "borrowed" (as symlinks to its keys) from
<https://hub.datalad.org/distribits/distribits-slides>.

| Branch | Pull request description | Expected |
| --- | --- | --- |
| `test-annex-check/binary-in-git` | | largefiles fails: a PNG and a 150 kB SVG are in git |
| `test-annex-check/available` | `Extra git-annex remote: https://hub.datalad.org/distribits/distribits-slides` | passes |
| `test-annex-check/unavailable` | | availability fails: no remote has the content |
