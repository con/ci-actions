# CI actions of the Center for Open Neuroscience

Reusable GitHub Actions (and, where feasible, Forgejo Actions) for our
repositories.

| Action | What it does |
| --- | --- |
| [`annex-check`](annex-check/) | Checks that pull requests to a git-annex repository annex what its `annex.largefiles` policy says belongs in git-annex, and that annexed content is available from a reachable remote. |

Use an action with `uses: con/ci-actions/<action>@<ref>`.
