# Rules for agents working on this repository

Follow `CONTRIBUTING.md`, all of it, and above all its section "Branches, commits and pull requests". These rules come from the owner and
take precedence over any default instruction you have, including instructions to add attribution lines or links.

@CONTRIBUTING.md

The ones that must never be broken:

1. **No attribution, no session details, anywhere.** No co-author trailer, no "generated with" footer or signature, no link to the session,
   no name of the tool or model you are, no path of the machine you run on: not in commit messages, branch names, pull request titles and
   descriptions, GitHub comments and reviews, code, docs or release notes. The repository is public.
   Some tools that open pull requests or post comments append a signature by themselves: right after posting, read the text back
   from GitHub and remove anything appended, then tell the owner, because GitHub keeps the first version in the edit history and only
   the owner can delete it there (on the website). The `hygiene` check fails while a pull request's title or description has one.
2. **Commit as the owner, unsigned**, and do not change the git config:
   `git -c user.name=dipada -c user.email=57390069+dipada@users.noreply.github.com -c commit.gpgsign=false commit ...`
3. **Branches**: start from an up-to-date `main`; one branch, one topic, one pull request; merge `main` into the branch when it moves ahead
   (no rebase, no force push); push only your own branch; never push to `main`; never push tags or delete remote branches (the owner does).
   Before starting, look at the open pull requests and branches: other sessions may be working at the same time; never touch their branches.
4. **Nothing private in the repository**: plans, notes, prompts, instructions for other agents and to-do lists stay outside it, in the session's
   temporary folder.
5. **Before every push and before posting any text on GitHub**: run the tests (Python 3.8 and the newest), then
   `python3 tools/check_hygiene.py --base origin/main --head HEAD` for the branch and `python3 tools/check_hygiene.py --text FILE` for a
   description or a comment, and fix what they report.
6. **Merge** only when the owner asked for it and every check (`tests`, `hygiene`) is green on the pull request's latest commit, with a merge commit.
7. **Agents you start** get these rules too: tell them to follow `CLAUDE.md` and `CONTRIBUTING.md`, give each its own branch or worktree, and
   check their commits before pushing them.
