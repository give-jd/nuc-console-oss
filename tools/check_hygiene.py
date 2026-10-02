#!/usr/bin/env python3
"""What a pull request publishes stays clean: who wrote its commits, and what its commits, its title, its description and the lines
it adds say (CONTRIBUTING.md, "Branches, commits and pull requests").

    python3 tools/check_hygiene.py --base origin/main --head HEAD     # the commits of a branch and the lines it adds
    python3 tools/check_hygiene.py --text draft.md                    # a description or a comment before posting it
    python3 tools/check_hygiene.py --event "$GITHUB_EVENT_PATH"       # CI: a pull_request event (base, head, title, description)

The findings say where and why, never the text found: CI logs are public, and a finding must not publish what it refuses.
Exit 0 when nothing is found, 1 with one line per finding, 2 on a usage error. Standard library only, Python 3.8+.
"""
import argparse
import json
import re
import subprocess
import sys

# The repository's two authors; a committer may also be GitHub itself (a merge or an edit made on the website).
AUTHORS = {"57390069+dipada@users.noreply.github.com", "4461018+give-jd@users.noreply.github.com"}
COMMITTERS = AUTHORS | {"noreply@github.com"}

# Nothing names the tools a change was made with, where it was made, or who else "wrote" it. CLAUDE.md, the file's name, is allowed.
FORBIDDEN = [(re.compile(p, re.I | re.M), why) for p, why in (
    (r"^[ \t]*co-authored-by[ \t]*:", "a co-author trailer"),
    (r"generated (?:with|by)\W{0,3}\[[^\]]*\]\(\s*https?://", "a generated-with footer"),
    (r"session_[0-9a-z]{16,}", "a session id"),
    (r"/code/session", "a link to a session"),
    (r"scratchpad|/tmp/claude-|/\.ccr/", "a path of the machine the change was made on"),
    (r"\bclaude\b(?!\.md)", "the name of a tool the change was made with"),
    (r"\banthropic\b", "the name of a tool the change was made with"),
    (r"\b(?:sonnet|haiku|opus)\b", "the name of a model"),
    (r"\bsub-?agents?\b", "how the work was split between agents"),
)]

# The lines a change adds are checked too, except in the two files that must spell the patterns out.
OWN_FILES = {"tools/check_hygiene.py", "tests/test_hygiene.py"}


def text_findings(text, where):
    """One finding per forbidden thing in `text`, with the line it is on."""
    out = []
    for rx, why in FORBIDDEN:
        for m in rx.finditer(text or ""):
            line = text[:m.start()].count("\n") + 1
            out.append("%s, line %d: %s" % (where, line, why))
    return out


def commit_findings(commits):
    """commits: [{"sha", "author", "committer", "message"}] -> findings (unknown author or committer, forbidden text)."""
    out = []
    for c in commits:
        short = c["sha"][:7]
        if c["author"].lower() not in AUTHORS:
            out.append("commit %s: author %s is not one of the repository's authors" % (short, c["author"]))
        if c["committer"].lower() not in COMMITTERS:
            out.append("commit %s: committer %s is not one of the repository's authors" % (short, c["committer"]))
        out += text_findings(c["message"], "commit %s message" % short)
    return out


def diff_findings(diff):
    """A unified diff (git diff -U0) -> findings in the lines it adds, file by file; the checker and its tests are skipped."""
    out, path, line, prev = [], None, 0, ""
    for raw in diff.splitlines():
        header, prev = prev.startswith("--- ") and raw.startswith("+++ "), raw
        if header:
            path = raw[6:] if raw.startswith("+++ b/") else None  # +++ /dev/null: a deleted file
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path and path not in OWN_FILES:
            for f in text_findings(raw[1:], path):
                out.append(re.sub(r", line \d+:", ", line %d:" % line, f, count=1))
            line += 1
        elif raw.startswith(" "):
            line += 1
    return out


def git(*args):
    return subprocess.run(("git",) + args, check=True, stdout=subprocess.PIPE, encoding="utf-8").stdout


def branch_commits(base, head):
    sep, end = "\x1f", "\x1e"
    log = git("log", "--format=%H" + sep + "%ae" + sep + "%ce" + sep + "%B" + end, "%s..%s" % (base, head))
    commits = []
    for rec in log.split(end):
        rec = rec.strip("\n")
        if rec:
            sha, author, committer, message = rec.split(sep, 3)
            commits.append({"sha": sha, "author": author, "committer": committer, "message": message})
    return commits


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="the branch the change goes into (origin/main); with --head: its commits and added lines")
    ap.add_argument("--head", help="the tip of the change (HEAD)")
    ap.add_argument("--text", action="append", default=[], help="a file to check as it is (a description or a comment); repeatable")
    ap.add_argument("--event", help="a GitHub pull_request event file: its base, head, title and description")
    a = ap.parse_args(argv)
    texts = []
    if a.event:
        with open(a.event, encoding="utf-8") as f:
            pr = json.load(f)["pull_request"]
        a.base, a.head = pr["base"]["sha"], pr["head"]["sha"]
        texts = [("pull request title", pr.get("title") or ""), ("pull request description", pr.get("body") or "")]
    if bool(a.base) != bool(a.head) or not (a.base or a.text):
        ap.print_usage(sys.stderr)
        return 2
    found = []
    if a.base:
        found += commit_findings(branch_commits(a.base, a.head))
        try:
            diff = git("diff", "-U0", "--no-color", "%s...%s" % (a.base, a.head))
        except subprocess.CalledProcessError:  # no common ancestor (a branch started from nothing): all of it is added
            diff = git("diff", "-U0", "--no-color", a.base, a.head)
        found += diff_findings(diff)
    for where, text in texts:
        found += text_findings(text, where)
    for path in a.text:
        with open(path, encoding="utf-8") as f:
            found += text_findings(f.read(), path)
    for f in found:
        print(f)
    if found:
        print("%d finding(s): see CONTRIBUTING.md, \"Branches, commits and pull requests\"" % len(found), file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
