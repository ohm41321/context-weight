"""Tests for cw.py on a synthetic HOME. Run: python3 -m unittest discover -s tests -v"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

CW = Path(__file__).resolve().parents[1] / "skills" / "context-weight" / "scripts" / "cw.py"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def rules(tag: str, n: int) -> str:
    """`n` distinct lines of about 100 bytes, so a cut removes real lines, as in a real file."""
    return "".join(f"{tag} {i:05d} " + "x" * 90 + "\n" for i in range(n))


def jsonl(path: Path, records) -> None:
    write(path, "".join(json.dumps(r) + "\n" for r in records))


class CwTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.tmp_path = t
        self.home = t / "home"
        self.repo = t / "work" / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DISABLE_CLAUDE_CODE",
                                 "SLASH_COMMAND_TOOL_CHAR_BUDGET", "BROWSER")}
        self.env["HOME"] = str(self.home)
        self.env["USERPROFILE"] = str(self.home)  # Windows: expanduser reads USERPROFILE, not HOME
        self.env["PYTHONIOENCODING"] = "utf-8"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cw(self, *args, code=0):
        p = subprocess.run([sys.executable, str(CW), *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout

    def cw_json(self, *args, code=0):
        return json.loads(self.run_cw(*args, "--json", code=code))

    # ------------------------------------------------------------------ files
    def test_claude_imports_rules_and_lazy_subdirs(self):
        write(self.home / ".claude" / "CLAUDE.md", "global rules\n@extra.md\n")
        write(self.home / ".claude" / "extra.md", "x" * 300)
        write(self.repo / "CLAUDE.md", "mail me a@b.com\nuse `@not.md` literally\n@docs/big.md\n")
        write(self.repo / "not.md", "never imported")
        write(self.repo / "docs" / "big.md", "y" * 30_000)
        write(self.repo / ".claude" / "rules" / "eager.md", "always on")
        write(self.repo / ".claude" / "rules" / "scoped.md", "---\npaths:\n  - src/**\n---\nonly for src")
        write(self.repo / "sub" / "CLAUDE.md", "z" * 9_000)  # loads on demand, not at launch

        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        names = [Path(f["path"]).name for f in r["files"]]
        self.assertEqual(names, ["CLAUDE.md", "extra.md", "CLAUDE.md", "big.md", "eager.md"])
        self.assertNotIn("not.md", names)
        self.assertEqual(r["files"][3]["role"], "project import")
        self.assertEqual(r["files"][3]["tokens"], 10_000)          # 30k ASCII chars / 3
        self.assertTrue(any("imports load at launch" in w for w in r["warnings"]))
        self.assertTrue(any("1 path-scoped" in n for n in r["notes"]))
        self.assertEqual(r["repo_tokens"], sum(f["tokens"] for f in r["files"][2:]))

    def test_thai_estimate_counts_one_token_per_char(self):
        write(self.repo / "CLAUDE.md", "ก" * 900 + "a" * 300)
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual(r["files"][0]["tokens"], 1_000)

    def test_large_claude_md_warning(self):
        write(self.repo / "CLAUDE.md", "rule\n" * 9_000)  # 45,000 chars
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertTrue(any("Large CLAUDE.md" in w for w in r["warnings"]))

    def test_codex_truncation_override_and_global(self):
        write(self.home / ".codex" / "AGENTS.md", "global codex")
        write(self.repo / "AGENTS.md", "a" * 40 * 1024)
        pkg = self.repo / "pkg"
        write(pkg / "AGENTS.override.md", "override wins")
        write(pkg / "AGENTS.md", "ignored because override exists")
        r = self.cw_json("files", str(pkg), "--harness", "codex")["codex"]
        self.assertEqual([f["role"] for f in r["files"]], ["global", "project", "project"])
        self.assertEqual(Path(r["files"][2]["path"]).name, "AGENTS.override.md")
        self.assertEqual(r["files"][1]["truncated_bytes"], 40 * 1024 - 32 * 1024)
        self.assertEqual(r["files"][2]["truncated_bytes"], len("override wins"))  # budget already spent
        self.assertTrue(any("never sees" in w for w in r["warnings"]))

    def test_codex_config_raises_limit(self):
        write(self.home / ".codex" / "config.toml", "project_doc_max_bytes = 65536\n")
        write(self.repo / "AGENTS.md", "a" * 40 * 1024)
        r = self.cw_json("files", str(self.repo), "--harness", "codex")["codex"]
        self.assertNotIn("truncated_bytes", r["files"][0])

    def test_cross_harness_divergence(self):
        write(self.repo / "CLAUDE.md", "c" * 30_000)
        write(self.repo / "AGENTS.md", "short")
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        self.assertTrue(res.get("cross"))
        write(self.repo / "CLAUDE.md", "@AGENTS.md\nclaude only line\n")
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        self.assertFalse(res.get("cross"))

    def test_cross_harness_one_side_empty(self):
        # The worst divergence: one harness gets every rule, the other gets none.
        write(self.repo / "CLAUDE.md", "c" * 30_000)
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        self.assertTrue(res.get("cross"), "Claude rules with no AGENTS.md went unflagged")
        self.assertIn("Codex ~0", res["cross"][0])
        # Codex was not measured, so nothing can be compared.
        res = self.cw_json("files", str(self.repo), "--harness", "claude")
        self.assertFalse(res.get("cross"))
        (self.repo / "CLAUDE.md").unlink()
        write(self.repo / "AGENTS.md", "a" * 30_000)
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        self.assertTrue(res.get("cross"), "AGENTS.md with no CLAUDE.md went unflagged")
        (self.repo / "AGENTS.md").unlink()
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        self.assertFalse(res.get("cross"))

    def test_gemini_context_filenames(self):
        write(self.home / ".gemini" / "settings.json", json.dumps({"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}}))
        write(self.repo / "AGENTS.md", "agents")
        write(self.repo / "GEMINI.md", "gemini")
        r = self.cw_json("files", str(self.repo), "--harness", "gemini")["gemini"]
        self.assertEqual(sorted(Path(f["path"]).name for f in r["files"]), ["AGENTS.md", "GEMINI.md"])

    def test_opencode_fallbacks(self):
        write(self.home / ".claude" / "CLAUDE.md", "claude global")
        write(self.repo / "CLAUDE.md", "claude project")
        r = self.cw_json("files", str(self.repo), "--harness", "opencode")["opencode"]
        self.assertEqual([f["role"] for f in r["files"]], ["global (CLAUDE.md fallback)", "project (CLAUDE.md fallback)"])
        write(self.repo / "AGENTS.md", "agents wins")
        r = self.cw_json("files", str(self.repo), "--harness", "opencode")["opencode"]
        self.assertEqual(Path(r["files"][1]["path"]).name, "AGENTS.md")

    def test_max_tokens_gate(self):
        write(self.repo / "CLAUDE.md", "a" * 3_000)  # ~1000 tokens
        self.run_cw("files", str(self.repo), "--harness", "claude", "--max-tokens", "2000")
        self.run_cw("files", str(self.repo), "--harness", "claude", "--max-tokens", "500", code=1)

    # ------------------------------------------------------------------ skills
    def test_skills_listing_and_budget(self):
        write(self.home / ".claude" / "skills" / "big" / "SKILL.md",
              "---\nname: big\ndescription: >\n  " + "d" * 5_000 + "\n---\nbody")
        write(self.home / ".claude" / "skills" / ".trash" / "SKILL.md", "---\nname: trash\n---\n")
        write(self.home / ".agents" / "skills" / "one" / "SKILL.md", "---\nname: one\ndescription: " + "e" * 600 + "\n---\n")
        res = self.cw_json("skills", str(self.repo), "--harness", "claude,codex", "--window", "10000")
        self.assertEqual(res["claude"]["count"], 1)
        self.assertEqual(res["claude"]["desc_chars"], 1_536)      # truncated in the listing
        self.assertTrue(res["claude"]["over_budget"])             # budget 100 tokens
        self.assertEqual(res["codex"]["count"], 1)
        self.assertTrue(res["codex"]["over_budget"])              # budget 200 tokens

    # ------------------------------------------------------------------ sessions
    def _claude_log(self, name, cwd, ts, ctxs, dup_first=0):
        recs = [{"type": "user", "cwd": str(cwd), "timestamp": ts}]
        for i, c in enumerate(ctxs):
            rec = {"type": "assistant", "cwd": str(cwd), "timestamp": ts, "isSidechain": False,
                   "message": {"id": f"{name}-m{i}", "usage": {"input_tokens": 1, "cache_read_input_tokens": c - 1,
                                                               "cache_creation_input_tokens": 0}}}
            recs += [rec] * (1 + (dup_first if i == 0 else 0))
        recs.append({"type": "assistant", "isSidechain": True, "timestamp": ts,
                     "message": {"id": "side", "usage": {"input_tokens": 999_999}}})
        jsonl(self.home / ".claude" / "projects" / "-repo" / f"{name}.jsonl", recs)

    def _codex_log(self, name, cwd, ts, ctxs, subagent=False):
        recs = [{"type": "session_meta", "timestamp": ts,
                 "payload": {"cwd": str(cwd), "thread_source": "subagent" if subagent else "user"}}]
        total = 0
        for c in ctxs:
            total += c
            ev = {"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": total},
                "last_token_usage": {"input_tokens": c, "cached_input_tokens": c // 2},
                "model_context_window": 258_400}}}
            recs += [ev, ev]  # repeated rate-limit update carries the same totals
        jsonl(self.home / ".codex" / "sessions" / "2026" / "11" / "01" / f"rollout-{name}.jsonl", recs)

    def test_sessions_dedupe_and_stats(self):
        self._claude_log("s1", self.repo / "web", "2026-10-01T00:00:00Z", [50_000, 200_000], dup_first=3)
        self._claude_log("s2", self.repo, "2026-10-02T00:00:00Z", [70_000])
        self._codex_log("c1", self.repo, "2026-10-03T00:00:00Z", [20_000, 30_000])
        self._codex_log("c2", self.repo, "2026-10-03T00:00:00Z", [9_000_000], subagent=True)
        res = self.cw_json("sessions", "--threshold", "150000")
        cl = res[f"claude {self.repo.resolve()}"]                 # subdir cwd folds into the git root
        self.assertEqual((cl["sessions"], cl["requests"]), (2, 3))  # duplicates and sidechain dropped
        self.assertEqual(cl["baseline_median"], 60_000)
        self.assertEqual(cl["ctx_max"], 200_000)
        self.assertAlmostEqual(cl["over_threshold_tokens"], 200_000 / 320_000)
        cx = res[f"codex {self.repo.resolve()}"]
        self.assertEqual((cx["sessions"], cx["requests"], cx["ctx_max"]), (1, 2, 30_000))  # subagent skipped

    def test_compare_at(self):
        self._claude_log("old", self.repo, "2026-10-01T00:00:00Z", [100_000])
        self._claude_log("new", self.repo, "2026-10-20T00:00:00Z", [40_000])
        res = self.cw_json("sessions", "--harness", "claude", "--compare-at", "2026-10-15")
        key = f"claude {self.repo.resolve()}"
        self.assertEqual(res["before"][key]["baseline_median"], 100_000)
        self.assertEqual(res["after"][key]["baseline_median"], 40_000)

    def test_report_runs_and_finds(self):
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._claude_log("s", self.repo, "2099-01-01T00:00:00Z", [90_000, 95_000])
        out = self.run_cw("report", str(self.repo), "--days", "40000")
        self.assertIn("## Findings", out)
        self.assertIn("would have saved", out)

    # ------------------------------------------------------------------ before and after
    def _git(self, *args, date=None):
        env = dict(self.env, GIT_CONFIG_NOSYSTEM="1")
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        p = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                            "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                           cwd=str(self.repo), env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout.strip()

    def _git_repo(self):
        (self.repo / ".git").rmdir()  # the fake marker from setUp
        self._git("init", "-q")

    def _commit(self, msg, date):
        self._git("add", "-A")
        self._git("commit", "-q", "-m", msg, date=date)
        return self._git("rev-parse", "--short=7", "HEAD")

    def test_compare_uncommitted_slimming(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "@AGENTS.md\n")
        write(self.repo / "AGENTS.md", "keep tests green\n")
        self._claude_log("s", self.repo, "2026-01-05T00:00:00Z", [90_000])
        c = self.cw_json("report", str(self.repo), "--days", "40000")["compare"]
        self.assertEqual(c["found"], "uncommitted")
        self.assertEqual(c["files"]["claude"]["before"], 20_000)
        self.assertLess(c["files"]["claude"]["after"], 100)
        self.assertEqual((c["files"]["codex"]["before"], c["files"]["codex"]["after"] > 0), (0, True))
        self.assertIsNone(c["sessions"]["claude"]["after"])            # nothing ran on the new files yet
        self.assertEqual(c["sessions"]["claude"]["before"]["sessions"], 1)
        out = self.run_cw("report", str(self.repo), "--days", "40000")
        self.assertIn("## Before and after", out)
        self.assertIn("20.0k -> ", out)

    def test_compare_finds_the_slimming_commit_and_ignores_later_growth(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "@AGENTS.md\n")
        write(self.repo / "AGENTS.md", "keep tests green\n")
        write(self.repo / "docs" / "agent-notes" / "build.md", "a" * 59_000)
        slim = self._commit("slim", "2026-02-01T12:00:00+00:00")
        write(self.repo / "AGENTS.md", "keep tests green\nlesson: run the linter\n")  # a /retro line
        self._commit("retro", "2026-02-05T00:00:00+00:00")
        for i, day in enumerate(("10", "12", "14")):
            self._claude_log(f"b{i}", self.repo, f"2026-01-{day}T00:00:00Z", [90_000, 95_000])
            self._claude_log(f"a{i}", self.repo, f"2026-02-{day}T00:00:00Z", [30_000, 31_000])
        self._claude_log("x", self.repo, "2026-02-01T06:00:00Z", [91_000])  # same day, before the commit
        c = self.cw_json("report", str(self.repo), "--days", "40000")["compare"]
        self.assertEqual((c["found"], c["commit"]), ("commit", slim))
        self.assertTrue(c["at"].startswith("2026-02-01T12:00"))
        self.assertEqual(c["files"]["claude"]["before"], 20_000)
        self.assertLess(c["files"]["claude"]["after"], 100)
        b, a = c["sessions"]["claude"]["before"], c["sessions"]["claude"]["after"]
        self.assertEqual((b["sessions"], a["sessions"]), (4, 3))                 # x counts as before
        self.assertEqual((b["baseline_median"], a["baseline_median"]), (90_000, 30_000))  # of 90k, 90k, 90k, 91k
        out = self.run_cw("report", str(self.repo), "--days", "40000")
        self.assertIn(f"commit {slim}", out)
        self.assertNotIn("too early", out)

    def test_compare_flags_too_few_sessions_after(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        self._commit("slim", "2026-02-01T00:00:00+00:00")
        self._claude_log("b", self.repo, "2026-01-10T00:00:00Z", [90_000])
        self._claude_log("a", self.repo, "2026-02-10T00:00:00Z", [30_000])
        out = self.run_cw("report", str(self.repo), "--days", "40000")
        self.assertIn("too early", out)

    def test_compare_uses_the_merge_of_a_slimming_branch(self):
        # Team flow: slim on a branch, merge the PR later. The change reaches main at the merge.
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        self._git("checkout", "-q", "-b", "slim")
        write(self.repo / "CLAUDE.md", "short\n")
        self._commit("slim", "2026-01-20T00:00:00+00:00")
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--no-ff", "-m", "merge slim", "slim", date="2026-02-01T00:00:00+00:00")
        merge = self._git("rev-parse", "--short=7", "HEAD")
        slim = self._git("rev-parse", "--short=7", "slim")
        c = self.cw_json("report", str(self.repo), "--days", "40000")["compare"]
        # The split is the merge; the name is the branch commit that did the cut.
        self.assertEqual((c["found"], c["commit"], c["merge"], c["subject"]), ("commit", slim, merge, "slim"))
        self.assertTrue(c["at"].startswith("2026-02-01T00:00"))
        self.assertEqual(c["files"]["claude"]["before"], 20_000)
        out = self.run_cw("report", str(self.repo), "--days", "40000")
        self.assertIn(f'commit {slim} "slim", merged in {merge}', out)

    def _merge_branch(self, steps, harness, main=None):
        """Commit `steps` [(subject, {file: text})] on a branch, then `main` {file: text} on main,
        merge the branch, and return the compare."""
        self._git("checkout", "-q", "-b", "slim")
        shas = {}
        for i, (subject, files) in enumerate(steps):
            for name, text in files.items():
                write(self.repo / name, text)
            shas[subject] = self._commit(subject, f"2026-01-{10 + i}T00:00:00+00:00")
        self._git("checkout", "-q", "main")
        if main:
            for name, text in main.items():
                write(self.repo / name, text)
            self._commit("main grows", "2026-01-25T00:00:00+00:00")
        self._git("merge", "-q", "--no-ff", "-m", "merge", "slim", date="2026-02-01T00:00:00+00:00")
        return self.cw_json("report", str(self.repo), "--days", "40000", "--harness", harness)["compare"], shas

    def test_compare_names_the_branch_commit_whose_cut_landed(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        c, shas = self._merge_branch([("B1 slim hard", {"CLAUDE.md": "short\n"}),
                                      ("B2 put it back", {"CLAUDE.md": "a" * 60_000}),
                                      ("B3 trim a third", {"CLAUDE.md": "a" * 40_000}),
                                      ("B4 one more line", {"CLAUDE.md": "a" * 40_030})], "claude")
        self.assertEqual((c["commit"], c["subject"]), (shas["B3 trim a third"], "B3 trim a third"))

    def test_compare_names_the_branch_commit_for_the_agents_asked(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 30_000)
        write(self.repo / "AGENTS.md", "b" * 90_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        c, shas = self._merge_branch([("slim CLAUDE", {"CLAUDE.md": "short\n"}),
                                      ("trim AGENTS", {"AGENTS.md": "short\n"})], "claude")
        self.assertEqual(c["subject"], "slim CLAUDE")

    def test_compare_names_the_branch_commit_that_cut_the_asked_load_most(self):
        # "trim both" cuts more bytes, mostly in AGENTS.md; for Claude Code "slim CLAUDE" cut most.
        self._git_repo()
        write(self.repo / "CLAUDE.md", rules("a", 600))
        write(self.repo / "AGENTS.md", rules("b", 900))
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        c, _ = self._merge_branch([("trim both", {"AGENTS.md": "short\n", "CLAUDE.md": rules("a", 500)}),
                                   ("slim CLAUDE", {"CLAUDE.md": "short\n"})], "claude")
        self.assertEqual(c["subject"], "slim CLAUDE")

    def test_compare_does_not_name_the_removal_of_text_the_branch_added(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        c, shas = self._merge_branch([("B1 paste a log", {"CLAUDE.md": "a" * 120_000}),
                                      ("B2 remove the pasted log", {"CLAUDE.md": "a" * 60_000}),
                                      ("B3 real slim", {"CLAUDE.md": "a" * 3_000})], "claude")
        self.assertEqual((c["commit"], c["subject"]), (shas["B3 real slim"], "B3 real slim"))

    def test_compare_names_the_merge_when_no_branch_commit_cut_enough(self):
        # Small trims make the slimming together. The one big commit mostly removes text the
        # branch pasted in itself: none of them is the cut, so the merge is named.
        self._git_repo()
        write(self.repo / "CLAUDE.md", rules("a", 300))
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        steps = [("paste", {"CLAUDE.md": rules("a", 300) + rules("p", 40)}),
                 ("cut", {"CLAUDE.md": rules("a", 270)})]
        steps += [(f"trim {i}", {"CLAUDE.md": rules("a", 270 - 15 * i)}) for i in range(1, 5)]
        c, _ = self._merge_branch(steps, "claude")
        self.assertEqual((c["merge"], c["subject"]), (None, "merge"))

    def test_compare_names_the_cut_the_merge_kept_not_one_put_back(self):
        # B1 drops section B, B2 drops section A, B3 puts B back: the merge removes only A.
        self._git_repo()
        write(self.repo / "CLAUDE.md", rules("A", 300) + rules("B", 300))
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        c, shas = self._merge_branch([("B1 drop section B", {"CLAUDE.md": rules("A", 300)}),
                                      ("B2 drop section A", {"CLAUDE.md": "short\n"}),
                                      ("B3 put section B back", {"CLAUDE.md": rules("B", 300)})], "claude")
        self.assertEqual((c["commit"], c["subject"]), (shas["B2 drop section A"], "B2 drop section A"))

    def test_compare_names_the_branch_slim_when_main_grew_meanwhile(self):
        # Main added a rules file while the branch was open, so its load at the merge is higher.
        self._git_repo()
        write(self.repo / "CLAUDE.md", rules("a", 600))
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        c, shas = self._merge_branch([("B1 paste a log", {"CLAUDE.md": rules("a", 600) + rules("p", 600)}),
                                      ("B2 remove the pasted log", {"CLAUDE.md": rules("a", 600)}),
                                      ("B3 real slim", {"CLAUDE.md": rules("a", 30)})],
                                     "claude", main={".claude/rules/team.md": rules("t", 600)})
        self.assertEqual((c["commit"], c["subject"]), (shas["B3 real slim"], "B3 real slim"))

    def test_compare_names_a_single_branch_cut_when_main_grew_meanwhile(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", rules("a", 600))
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        c, shas = self._merge_branch([("B1 cut half", {"CLAUDE.md": rules("a", 300)})],
                                     "claude", main={".claude/rules/team.md": rules("t", 300)})
        self.assertEqual((c["commit"], c["subject"]), (shas["B1 cut half"], "B1 cut half"))
        self.assertIsNotNone(c["merge"])

    def test_compare_names_the_newer_of_two_equal_branch_cuts(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", rules("a", 900))
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        c, shas = self._merge_branch([("B1 cut a third", {"CLAUDE.md": rules("a", 600)}),
                                      ("B2 cut another third", {"CLAUDE.md": rules("a", 300)})], "claude")
        self.assertEqual((c["commit"], c["subject"]), (shas["B2 cut another third"], "B2 cut another third"))

    def test_compare_at_a_time_finds_the_cut_past_other_changes(self):
        # Five AGENTS.md cuts and a small trim come first; Claude Code's cut is the seventh
        # change, then it grew back.
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        write(self.repo / "AGENTS.md", rules("b", 400))
        self._commit("notes", "2026-08-01T00:00:00+00:00")
        for i in range(1, 6):
            write(self.repo / "AGENTS.md", rules("b", 400 - 70 * i))
            self._commit(f"codex {i}", f"2026-08-20T0{i}:00:00+00:00")
        write(self.repo / "CLAUDE.md", "a" * 54_000)  # a tenth: too small to be the slimming
        self._commit("trim", "2026-08-20T06:00:00+00:00")
        write(self.repo / "CLAUDE.md", "a" * 6_000)
        self._commit("slim", "2026-08-20T07:00:00+00:00")
        write(self.repo / "CLAUDE.md", "a" * 90_000)
        self._commit("grow", "2026-09-01T00:00:00+00:00")
        for hs in ("claude", "claude,codex"):
            args = ("report", str(self.repo), "--days", "40000", "--harness", hs, "--compare-at", "2026-08-20T00:30Z")
            f = self.cw_json(*args)["compare"]["files"]["claude"]
            self.assertEqual((f["before"], f["after"], f["now"]), (20_000, 2_000, 30_000), hs)
            self.assertNotIn("did not shrink", self.run_cw(*args))

    def test_compare_at_a_time_weighs_each_agent_at_its_own_cut(self):
        # After the split, the first cut is Codex's AGENTS.md, the next one Claude Code's CLAUDE.md.
        self._git_repo()
        write(self.repo / "CLAUDE.md", rules("a", 600))
        write(self.repo / "AGENTS.md", rules("c", 300))
        self._commit("rules", "2026-08-01T00:00:00+00:00")
        write(self.repo / "AGENTS.md", "short\n")
        self._commit("slim AGENTS", "2026-08-20T01:00:00+00:00")
        write(self.repo / "CLAUDE.md", rules("a", 60))
        self._commit("slim CLAUDE", "2026-08-20T02:00:00+00:00")
        write(self.repo / "AGENTS.md", rules("c", 300))
        self._commit("AGENTS grew back", "2026-09-01T00:00:00+00:00")
        args = ("report", str(self.repo), "--days", "40000", "--compare-at", "2026-08-20T00:30Z", "--harness")
        alone = {h: self.cw_json(*args, h)["compare"]["files"][h] for h in ("claude", "codex")}
        both = self.cw_json(*args, "claude,codex")["compare"]["files"]
        self.assertLess(alone["claude"]["after"], alone["claude"]["before"])
        self.assertLess(alone["codex"]["after"], alone["codex"]["now"])
        self.assertEqual(both, alone)
        changed = self.cw_json(*args, "claude,codex")["compare"]["changed"]
        self.assertEqual({x["path"]: x["after"] for x in changed},
                         {"CLAUDE.md": alone["claude"]["after"], "AGENTS.md": alone["codex"]["after"]})

    def test_compare_names_an_old_slimming_on_a_long_branch(self):
        # The slim comes before ten small edits, one of which drops a line: the slim is the cut.
        self._git_repo()
        line = "r" * 99 + "\n"
        write(self.repo / "CLAUDE.md", line * 600)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        steps = [("slim", {"CLAUDE.md": line * 20})]
        for i in range(10):
            n = 21 + i if i != 5 else 24                                  # T5 drops one line
            steps.append((f"T{i}", {"CLAUDE.md": line * n}))
        c, shas = self._merge_branch(steps, "claude")
        self.assertEqual((c["commit"], c["subject"]), (shas["slim"], "slim"))

    def test_compare_names_the_slimming_when_the_branch_merged_an_older_one(self):
        # The slim branch merged a side branch cut from main before the slim; the slim still names.
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        self._git("checkout", "-q", "-b", "side")
        write(self.repo / ".claude" / "rules" / "x.md", "rule\n")
        self._commit("side rule", "2026-01-05T00:00:00+00:00")
        self._git("checkout", "-q", "main")
        self._git("checkout", "-q", "-b", "slim")
        write(self.repo / "CLAUDE.md", "short\n")
        slim = self._commit("B1 slim", "2026-01-10T00:00:00+00:00")
        self._git("merge", "-q", "--no-ff", "-m", "take side", "side", date="2026-01-12T00:00:00+00:00")
        self._git("checkout", "-q", "main")
        self._git("merge", "-q", "--no-ff", "-m", "merge", "slim", date="2026-02-01T00:00:00+00:00")
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual((c["commit"], c["subject"]), (slim, "B1 slim"))

    def test_compare_at_a_time_skips_changes_that_do_not_load(self):
        # Between the split and the slim: a subfolder CLAUDE.md and an AGENTS.md Claude never reads.
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-08-01T00:00:00+00:00")
        write(self.repo / "pkg" / "CLAUDE.md", "pkg rules\n")
        self._commit("pkg", "2026-08-19T00:10:00+00:00")
        write(self.repo / "AGENTS.md", "codex rules\n")
        self._commit("codex", "2026-08-19T00:20:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        self._commit("slim", "2026-08-20T00:24:00+00:00")
        args = ("report", str(self.repo), "--days", "40000", "--harness", "claude", "--compare-at", "2026-08-19T00:00Z")
        f = self.cw_json(*args)["compare"]["files"]["claude"]
        self.assertEqual((f["before"], f["after"]), (20_000, 2))
        self.assertNotIn("did not shrink", self.run_cw(*args))

    def test_compare_at_takes_a_sha256_commit_id(self):
        (self.repo / ".git").rmdir()
        if subprocess.run(["git", "init", "-q", "--object-format=sha256", str(self.repo)],
                          capture_output=True).returncode:
            self.skipTest("this git has no SHA-256 repositories")
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        self._commit("slim", "2026-02-01T00:00:00+00:00")
        full = self._git("rev-parse", "HEAD")
        self.assertEqual(len(full), 64)
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude", "--compare-at", full)["compare"]
        self.assertEqual(c["files"]["claude"]["before"], 20_000)

    def test_compare_finds_a_slimming_that_grew_back(self):
        # Slimmed once, then grew past the old size: still the slimming, judged against its parent.
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 37_758)
        self._commit("notes", "2026-08-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "a" * 5_595)
        slim = self._commit("split project notes into docs/", "2026-08-20T00:24:00+00:00")
        write(self.repo / "CLAUDE.md", "a" * 114_999)
        write(self.repo / "AGENTS.md", "b" * 30_000)                    # new for Codex: growth, never cut
        self._commit("grow", "2026-09-15T00:00:00+00:00")
        args = ("report", str(self.repo), "--days", "40000", "--harness", "claude,codex")
        c = self.cw_json(*args)["compare"]
        self.assertEqual((c["found"], c["commit"]), ("commit", slim))
        f = c["files"]["claude"]
        self.assertEqual((f["before"], f["after"], f["now"]), (12_586, 1_865, 38_333))
        self.assertEqual([(x["before"], x["after"]) for x in c["changed"]], [(12_586, 1_865)])  # what the commit did
        out = self.run_cw(*args)
        self.assertIn("12.6k -> 1.9k, now 38.3k", out)
        grew = next(ln for ln in out.splitlines() if ln.startswith("Grew back"))
        self.assertEqual(grew, "Grew back since the change: Claude Code ~1.9k -> ~38.3k now, past the ~12.6k "
                               "before it. Sessions after it include that growth.")

    def test_compare_commit_subject_has_no_control_characters(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        self._commit("slim \x1b[2J\x1b]0;owned\x07 notes\u202e" + " and more" * 10, "2026-02-01T00:00:00+00:00")
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual(c["subject"], "slim [2J]0;owned notes" + " and more" * 10)  # whole, in the JSON
        out = self.run_cw("report", str(self.repo), "--days", "40000", "--harness", "claude")
        self.assertNotRegex(out, "[\x00-\x08\x0b-\x1f\x7f\u202e]")
        self.assertIn("notes and more and more", out)
        self.assertIn('…"', out)                                          # shortened in the label

    def test_compare_at_takes_a_time_or_a_commit(self):
        self._git_repo()
        write(self.repo / "AGENTS.md", "rules\n")
        self._commit("start", "2026-08-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("notes", "2026-08-19T23:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        slim = self._commit("slim", "2026-08-20T00:24:00+00:00")
        write(self.repo / "CLAUDE.md", "a" * 120_000)
        self._commit("grow", "2026-09-01T00:00:00+00:00")
        args = ("report", str(self.repo), "--days", "40000", "--harness", "claude")
        c = self.cw_json(*args, "--compare-at", "2026-08-20T00:20+00:00")["compare"]  # just before the slim
        f = c["files"]["claude"]
        self.assertEqual((c["found"], f["before"], f["after"], f["now"]), ("date", 20_000, 2, 40_000))
        self.assertTrue(c["at"].startswith("2026-08-20T00:20"))
        self.assertNotIn("did not shrink", self.run_cw(*args, "--compare-at", "2026-08-20T00:20+00:00"))
        c = self.cw_json(*args, "--compare-at", "2150-01-01")["compare"]  # git reads no date that late
        self.assertEqual(c["files"]["claude"]["before"], 40_000)
        c = self.cw_json(*args, "--compare-at", "2026-08-19 22:00Z")["compare"]
        self.assertEqual(c["files"]["claude"]["before"], 0)          # no CLAUDE.md yet
        self.assertEqual(c["files"]["claude"]["after"], 40_000)      # no cut since: today
        self.assertIn("did not shrink", self.run_cw(*args, "--compare-at", "2026-08-19 22:00Z"))
        c = self.cw_json(*args, "--compare-at", slim)["compare"]
        self.assertEqual((c["found"], c["commit"], c["picked"]), ("commit", slim, True))
        self.assertTrue(c["at"].startswith("2026-08-20T00:24"))
        f = c["files"]["claude"]
        self.assertEqual((f["before"], f["after"], f["now"]), (20_000, 2, 40_000))
        for bad in ("yesterday", "2026-08-20T25:00", "ffffffffff", "--all"):
            self.run_cw(*args, "--compare-at", bad, code=2)

    def test_compare_says_when_claude_deleted_old_logs(self):
        self._git_repo()
        self._slim_history()                                             # slim on 2026-02-01
        for i, day in enumerate(("10", "12", "14")):
            self._claude_log(f"a{i}", self.repo, f"2026-02-{day}T12:00:00Z", [30_000])  # local day in any zone
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual(c["logs_from"], {"claude": "2026-02-10"})
        out = self.run_cw("report", str(self.repo), "--days", "40000", "--harness", "claude")
        self.assertIn("cleanupPeriodDays", out)
        self.assertIn("30 days", out)
        self.assertEqual((c["cleanup_days"], c["logs_deleted"]), (30, True))
        write(self.home / ".claude" / "settings.json", '{"cleanupPeriodDays": 100000}')
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual((c["logs_from"], c["cleanup_days"], c["logs_deleted"]), ({"claude": "2026-02-10"}, 100000, False))
        out = self.run_cw("report", str(self.repo), "--days", "40000", "--harness", "claude")
        self.assertNotIn("deletes transcripts", out)
        self.assertIn("logs on this machine start 2026-02-10", out)
        os.remove(str(self.home / ".claude" / "settings.json"))
        self._claude_log("b", self.repo, "2026-01-10T00:00:00Z", [90_000])
        for i, day in enumerate(("11", "12")):
            self._claude_log(f"b{i}", self.repo, f"2026-01-{day}T00:00:00Z", [90_000])
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertNotIn("logs_from", c)                                 # enough sessions before

    def test_compare_at_date_and_no_compare_without_a_cut(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 3_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "a" * 3_300)                      # growth, not a cut
        self._commit("more", "2026-01-10T00:00:00+00:00")
        self.assertNotIn("compare", self.cw_json("report", str(self.repo), "--days", "40000"))
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--compare-at", "2026-01-05")["compare"]
        self.assertEqual(c["found"], "date")
        self.assertEqual((c["files"]["claude"]["before"], c["files"]["claude"]["after"]), (1_000, 1_100))
        out = self.run_cw("report", str(self.repo), "--days", "40000", "--compare-at", "2026-01-05")
        self.assertIn("did not shrink at this split", out)

    def test_compare_skips_a_move_into_an_imported_file(self):
        # The entry file shrinks, but @import still loads every line: nothing to compare.
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "@docs/rules.md\n")
        write(self.repo / "docs" / "rules.md", "a" * 60_000)
        self._commit("move", "2026-02-01T00:00:00+00:00")
        self.assertNotIn("compare", self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude"))

    @unittest.skipIf(os.name == "nt", "the hook below is a POSIX shell command")
    def test_compare_never_runs_the_repo_fsmonitor_hook(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        ran = self.tmp_path / "hook-ran"
        self._git("config", "core.fsmonitor", f"touch {ran}; false")
        c = self.cw_json("report", str(self.repo), "--days", "40000")["compare"]
        self.assertEqual(c["found"], "uncommitted")
        self.assertFalse(ran.exists())

    def _slim_history(self):
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        return self._commit("slim", "2026-02-01T00:00:00+00:00")

    def _marker_script(self, name):
        ran = self.tmp_path / f"{name}-ran"
        script = write(self.tmp_path / f"{name}.sh", f"#!/bin/sh\ntouch '{ran}'\nexit 1\n")
        script.chmod(0o755)
        return script, ran

    def test_compare_follows_imports_that_are_not_markdown(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "See @package.json\n" + "a" * 30_000)
        write(self.repo / "package.json", "b" * 9_000)
        old = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]["repo_tokens"]
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        self._commit("slim", "2026-02-01T00:00:00+00:00")
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual(c["files"]["claude"]["before"], old)
        self.assertEqual({x["path"]: (x["before"], x["after"]) for x in c["changed"]}["package.json"],
                         (3_000, 0))  # weighed at the old commit, no longer imported now

    def test_compare_copy_never_leaves_its_folder(self):
        spec = importlib.util.spec_from_file_location("cw_under_test", str(CW))
        cw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cw)
        for bad in ("..\\..\\.claude\\CLAUDE.md", "\\Users\\me\\.claude\\CLAUDE.md", "C:/x/CLAUDE.md",
                    "a/../../CLAUDE.md", "/etc/CLAUDE.md", "a//CLAUDE.md", "./CLAUDE.md"):
            self.assertFalse(cw._rel_ok(bad), bad)
        self.assertTrue(cw._rel_ok("docs/sub/CLAUDE.md"))

    @unittest.skipIf(os.name == "nt", "needs POSIX symlinks")
    def test_compare_reads_a_linked_rules_folder_in_the_old_commit(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "core\n")
        write(self.repo / "shared" / "rules" / "big.md", "a" * 60_000)
        (self.repo / ".claude").mkdir()
        os.symlink("../shared/rules", str(self.repo / ".claude" / "rules"))
        old = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]["repo_tokens"]
        self.assertGreater(old, 20_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        os.remove(str(self.repo / ".claude" / "rules"))
        self._commit("slim", "2026-02-01T00:00:00+00:00")
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual(c["files"]["claude"]["before"], old)

    @unittest.skipIf(os.name == "nt", "the hook below is a POSIX shell script")
    def test_compare_never_runs_a_signature_program(self):
        self._git_repo()
        slim = self._slim_history()
        raw = self._git("cat-file", "commit", "HEAD")
        head, _, msg = raw.partition("\n\n")
        signed = head + "\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n -----END PGP SIGNATURE-----\n\n" + msg + "\n"
        p = subprocess.run(["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=str(self.repo),
                           input=signed, capture_output=True, text=True, env=self.env)
        self._git("update-ref", "refs/heads/main", p.stdout.strip())
        script, ran = self._marker_script("gpg")
        self._git("config", "log.showSignature", "true")
        self._git("config", "gpg.program", str(script))
        c = self.cw_json("report", str(self.repo), "--days", "40000")["compare"]
        self.assertEqual(c["found"], "commit")
        self.assertFalse(ran.exists())

    @unittest.skipIf(os.name == "nt", "the hook below is a POSIX shell script")
    def test_compare_never_fetches_in_a_partial_clone(self):
        self._git_repo()
        self._slim_history()
        self._git("config", "uploadpack.allowFilter", "true")
        clone = self.tmp_path / "work" / "clone"
        self._git("clone", "-q", "--filter=blob:none", "--no-local", self.repo.as_uri(), str(clone))
        script, ran = self._marker_script("ssh")
        self._git("-C", str(clone), "remote", "set-url", "origin", "ssh://example.invalid/x")
        self._git("-C", str(clone), "config", "core.sshCommand", str(script))
        self._git("-C", str(clone), "config", "protocol.ssh.allow", "always")  # beats protocol.allow=never
        packs = sorted(os.listdir(str(clone / ".git" / "objects" / "pack")))
        res = self.cw_json("report", str(clone), "--days", "40000")
        self.assertFalse(ran.exists())
        self.assertEqual(sorted(os.listdir(str(clone / ".git" / "objects" / "pack"))), packs)
        self.assertNotIn("compare", res)  # the old blobs are not here, so there is nothing honest to show

    @unittest.skipIf(os.name == "nt", "a POSIX stand-in for git.exe in the current folder")
    def test_compare_never_runs_a_git_from_the_current_folder(self):
        self._git_repo()
        self._slim_history()
        here = self.tmp_path / "untrusted"
        here.mkdir()
        fake = write(here / "git", f"#!/bin/sh\ntouch '{here / 'fake-git-ran'}'\nexit 1\n")
        fake.chmod(0o755)
        env = dict(self.env, PATH="." + os.pathsep + self.env.get("PATH", ""))
        p = subprocess.run([sys.executable, str(CW), "report", str(self.repo), "--days", "40000", "--json"],
                           cwd=str(here), env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse((here / "fake-git-ran").exists())
        self.assertEqual(json.loads(p.stdout)["compare"]["found"], "commit")

    def test_compare_at_out_of_range_exits_2(self):
        self._git_repo()
        self._slim_history()
        self.run_cw("report", str(self.repo), "--compare-at", "0001-01-01", code=2)

    def _load_at_commit(self, *args):
        return self.cw_json("files", str(self.repo), *args)

    @unittest.skipIf(os.name == "nt", "needs POSIX symlinks")
    def test_compare_plain_link_checkout_is_not_a_change(self):
        # Git for Windows' default core.symlinks=false checks a link out as a text file.
        self._git_repo()
        write(self.repo / "AGENTS.md", "a" * 60_000)
        os.symlink("AGENTS.md", str(self.repo / "CLAUDE.md"))
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        clone = self.tmp_path / "work" / "plain"
        self._git("clone", "-q", "-c", "core.symlinks=false", str(self.repo), str(clone))
        self.assertFalse(os.path.islink(str(clone / "CLAUDE.md")))
        self.assertNotIn("compare", self.cw_json("report", str(clone), "--days", "40000"))

    @unittest.skipIf(os.name == "nt", "needs POSIX symlinks")
    def test_compare_links_elsewhere_do_not_hide_the_slimming(self):
        self._git_repo()
        slim = self._slim_history()
        write(self.repo / "AGENTS.md", "keep tests green\n")
        for i in range(4):  # links in subfolders never load from the root
            (self.repo / f"p{i}").mkdir()
            os.symlink("../AGENTS.md", str(self.repo / f"p{i}" / "CLAUDE.md"))
            self._commit(f"link {i}", f"2026-02-0{i + 2}T00:00:00+00:00")
        c = self.cw_json("report", str(self.repo), "--days", "40000")["compare"]
        self.assertEqual((c["found"], c["commit"]), ("commit", slim))

    def test_compare_opencode_globs_match_like_glob(self):
        self._git_repo()
        write(self.repo / "opencode.json", json.dumps({"instructions": ["docs/**/*.md"]}))
        write(self.repo / "docs" / "a.md", "a" * 30_000)
        write(self.repo / "docs" / "sub" / "b.md", "b" * 60_000)
        old = self._load_at_commit("--harness", "opencode")["opencode"]["repo_tokens"]
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "docs" / "sub" / "b.md", "short\n")
        self._commit("slim", "2026-02-01T00:00:00+00:00")
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "opencode")["compare"]
        self.assertEqual(c["files"]["opencode"]["before"], old)
        self.assertNotIn("docs/a.md", [x["path"] for x in c["changed"]])

    @unittest.skipIf(os.name == "nt", "needs POSIX symlinks")
    def test_compare_linked_file_imports_from_where_it_lives(self):
        self._git_repo()
        write(self.repo / "docs" / "AGENTS.md", "@rules.md\n")
        write(self.repo / "docs" / "rules.md", "a" * 60_000)
        os.symlink("docs/AGENTS.md", str(self.repo / "CLAUDE.md"))
        old = self._load_at_commit("--harness", "claude")["claude"]["repo_tokens"]
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        os.remove(str(self.repo / "CLAUDE.md"))
        write(self.repo / "CLAUDE.md", "short\n")
        self._commit("slim", "2026-02-01T00:00:00+00:00")
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual(c["files"]["claude"]["before"], old)
        self.assertNotIn("docs/rules.md", [x["path"] for x in c["changed"] if x["before"] == 0])

    def test_compare_a_move_into_an_ignored_local_file_is_not_a_slimming(self):
        self._git_repo()
        write(self.repo / ".gitignore", "CLAUDE.local.md\n")
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        write(self.repo / "CLAUDE.local.md", "a" * 60_000)
        self._commit("move", "2026-02-01T00:00:00+00:00")
        self.assertNotIn("compare", self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude"))

    @unittest.skipIf(os.name == "nt", "needs POSIX symlinks")
    def test_compare_folder_link_to_itself_counts_once(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "core\n")
        write(self.repo / ".claude" / "rules" / "a.md", "a" * 30_000)
        write(self.repo / ".claude" / "rules" / "b.md", "b" * 60_000)
        os.symlink(".", str(self.repo / ".claude" / "rules" / "again"))
        old = self._load_at_commit("--harness", "claude")["claude"]["repo_tokens"]
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        os.remove(str(self.repo / ".claude" / "rules" / "b.md"))
        self._commit("slim", "2026-02-01T00:00:00+00:00")
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual(c["files"]["claude"]["before"], old)

    def test_compare_section_in_the_picture(self):
        self._git_repo()
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "CLAUDE.md", "short\n")
        self._commit("slim", "2026-02-01T00:00:00+00:00")
        self._claude_log("b", self.repo, "2026-01-10T00:00:00Z", [90_000])
        out = self.tmp_path / "pic.svg"
        self.run_cw("report", str(self.repo), "--days", "40000", "--svg", str(out))
        _, texts = self._svg_texts(out)
        self.assertIn("Before and after", texts)
        self.assertIn("~20.0k → ~2", texts)
        self.assertIn("no sessions after the change yet", texts)

    # ------------------------------------------------------------------ picture
    def _svg_texts(self, path):
        root = ET.parse(str(path)).getroot()  # raises if the SVG is not well-formed XML
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        return root, " ".join("".join(t.itertext()) for t in root.iter("{http://www.w3.org/2000/svg}text"))

    def test_report_svg_draws_the_same_numbers(self):
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        write(self.repo / "AGENTS.md", "b" * 40_000)  # past Codex's 32 KiB default
        self._claude_log("s", self.repo, "2099-01-01T00:00:00Z", [90_000, 95_000])
        out = self.tmp_path / "pic.svg"
        res = self.cw_json("report", str(self.repo), "--days", "40000", "--svg", str(out), "--theme", "light")
        self.assertEqual(Path(res["picture"]), out)
        root, texts = self._svg_texts(out)
        self.assertEqual(root.get("role"), "img")
        self.assertIn("~20.0k", texts)                       # 60,000 ASCII chars / 3, as in `files`
        self.assertIn("Claude Code", texts)
        self.assertIn("Codex cuts 7,232 bytes", texts)
        self.assertIn("of Claude Code input", texts)
        self.assertIn("Requests", texts)
        self.assertIn("would have saved", texts)
        self.assertNotIn("prefers-color-scheme", out.read_text(encoding="utf-8"))
        text_out = self.run_cw("report", str(self.repo), "--days", "40000", "--svg", str(out))
        self.assertIn(f"Picture: {out}", text_out)
        self.assertIn("prefers-color-scheme: dark", out.read_text(encoding="utf-8"))  # auto follows the viewer

    def test_report_svg_hero_row_describes_one_agent(self):
        # Claude has the heavy file; Codex has more input. The tiles once mixed the two.
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        write(self.repo / "AGENTS.md", "short")
        self._claude_log("s", self.repo, "2099-01-01T00:00:00Z", [90_000, 95_000])
        self._codex_log("c", self.repo, "2099-01-01T00:00:00Z", [300_000, 300_000, 300_000])
        out = self.tmp_path / "pic.svg"
        self.cw_json("report", str(self.repo), "--days", "40000", "--svg", str(out))
        _, texts = self._svg_texts(out)
        self.assertIn("startup in Claude Code", texts)
        self.assertIn("median, Claude Code", texts)
        self.assertIn("of Claude Code input", texts)
        self.assertNotIn("median, Codex", texts)
        self.assertNotIn("of Codex input", texts)

    def test_report_svg_escapes_paths_and_works_without_sessions(self):
        repo = self.tmp_path / "work" / "R&D <x>" if os.name != "nt" else self.tmp_path / "work" / "R&D"
        (repo / ".git").mkdir(parents=True)
        write(repo / "CLAUDE.md", "rules\n")
        out = self.tmp_path / "pic.svg"
        self.run_cw("report", str(repo), "--svg", str(out))
        _, texts = self._svg_texts(out)
        self.assertIn("R&D", texts)
        self.assertIn("No session logs", texts)

    def test_report_svg_default_path_goes_to_temp(self):
        tmp = self.tmp_path / "tmpdir"
        tmp.mkdir()
        self.env.update(TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
        write(self.repo / "CLAUDE.md", "rules\n")
        res = self.cw_json("report", str(self.repo), "--svg")
        pic = Path(res["picture"]).resolve()
        self.assertIn(tmp.resolve(), pic.parents)
        self.assertEqual(pic.name, "context-weight-repo.svg")
        self._svg_texts(pic)
        again = Path(self.cw_json("report", str(self.repo), "--svg")["picture"]).resolve()
        self.assertNotEqual(pic.parent, again.parent)  # a fresh private folder each run

    @unittest.skipIf(os.name == "nt", "needs POSIX symlinks")
    def test_report_svg_default_path_ignores_a_planted_symlink(self):
        tmp = self.tmp_path / "tmpdir"
        tmp.mkdir()
        self.env.update(TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
        victim = write(self.tmp_path / "victim.txt", "keep me")
        for name in ("context-weight-repo.svg", "repo.svg"):
            os.symlink(str(victim), str(tmp / name))
        res = self.cw_json("report", str(self.repo), "--svg")
        self.assertEqual(victim.read_text(), "keep me")
        self._svg_texts(res["picture"])

    def test_svg_drops_characters_xml_cannot_hold(self):
        spec = importlib.util.spec_from_file_location("cw_under_test", str(CW))
        cw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cw)
        bad = "/x/caf\udce9 a\x01b\ufffe"
        rep = {"project": bad, "since": "2026-01-01", "days": 30, "target": 5000, "sessions": {},
               "files": {"claude": {"files": [{"path": bad + "/CLAUDE.md", "role": "project", "tokens": 9000}],
                                    "total_tokens": 9000, "repo_tokens": 9000, "warnings": [], "notes": []}},
               "skills": {"claude": {"budget": 8000, "budget_unit": "chars", "chars": 8320, "tokens": 2000,
                                     "window_source": "window"}},
               "findings": ["[claude] odd \x03 character"]}
        out = cw.write_picture(rep, str(self.tmp_path / "odd.svg"), "auto")
        _, texts = self._svg_texts(out)
        self.assertIn("4% over budget", texts)
        self.assertIn("odd \ufffd character", texts)

    def test_report_svg_unwritable_exits_2(self):
        bad = self.tmp_path / "no" / "such" / "dir" / "a.svg"
        p = subprocess.run([sys.executable, str(CW), "report", str(self.repo), "--svg", str(bad)],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2, p.stderr)
        self.assertIn("cannot write", p.stderr)

    @unittest.skipIf(os.name == "nt", "BROWSER command lines are POSIX-quoted")
    def test_report_open_hands_the_picture_to_a_viewer(self):
        seen = self.tmp_path / "opened.txt"
        viewer = write(self.tmp_path / "viewer.py",
                       "import sys, pathlib\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\n")
        self.env["BROWSER"] = f"{sys.executable} {viewer} {seen} %s"
        out = self.tmp_path / "pic.svg"
        self.run_cw("report", str(self.repo), "--svg", str(out), "--open")
        self.assertEqual(seen.read_text(), out.resolve().as_uri())

    def test_continued_session_counted_once_and_kept_out_of_baseline(self):
        def rec(mid, ts, ctx):
            return {"type": "assistant", "cwd": str(self.repo), "timestamp": ts, "isSidechain": False,
                    "message": {"id": mid, "usage": {"input_tokens": ctx}}}
        proj = self.home / ".claude" / "projects" / "-repo"
        jsonl(proj / "a.jsonl", [rec("m0", "2026-10-01T00:00:00Z", 40_000), rec("m1", "2026-10-01T00:01:00Z", 90_000),
                                 rec("m2", "2026-10-01T00:02:00Z", 130_000)])
        # written after compaction: header, boundary, copy of m2 (old timestamp), then new work
        jsonl(proj / "b.jsonl", [{"type": "ai-title"}, {"type": "system", "subtype": "compact_boundary",
                                                         "timestamp": "2026-10-01T01:00:00Z"},
                                 rec("m2", "2026-10-01T00:02:00Z", 130_000), rec("m3", "2026-10-01T01:01:00Z", 30_000)])
        s = self.cw_json("sessions", "--harness", "claude")[f"claude {self.repo.resolve()}"]
        self.assertEqual(s["requests"], 4)
        self.assertEqual(s["input_tokens"], 290_000)
        self.assertEqual(s["continued_sessions"], 1)
        self.assertEqual(s["baseline_median"], 40_000)

    @unittest.skipIf(os.name == "nt", "Windows ignores TZ names such as Asia/Bangkok")
    def test_dates_are_local_not_utc(self):
        self.env["TZ"] = "Asia/Bangkok"
        self._claude_log("late", self.repo, "2026-11-04T19:00:00Z", [10_000])  # 02:00 on 5 Nov in UTC+7
        res = self.cw_json("sessions", "--harness", "claude", "--since", "2026-11-05")
        self.assertIn(f"claude {self.repo.resolve()}", res)

    def test_import_syntax_matches_claude(self):
        write(self.repo / "CLAUDE.md", "@docs/arch.png\n@My\\ Notes.md\n@notes(v2).md\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "arch.png").write_bytes(b"\x89PNG\0\0" * 1000)
        write(self.repo / "My Notes.md", "spaced")
        write(self.repo / "notes(v2).md", "parens")
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual(sorted(Path(f["path"]).name for f in r["files"]),
                         ["CLAUDE.md", "My Notes.md", "notes(v2).md"])

    def test_auto_memory_counted_first_200_lines(self):
        slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(self.repo.resolve()))
        write(self.home / ".claude" / "projects" / slug / "memory" / "MEMORY.md", "abc\n" * 500)
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        mem = [f for f in r["files"] if f["role"] == "user memory"]
        self.assertEqual(len(mem), 1)
        self.assertEqual(mem[0]["lines"], 200)

    # ------------------------------------------------------------------ verify-split
    def test_verify_split(self):
        old = write(self.repo / "old.md", "# Title\n\nrule one\nrule  two\n---\nhistory line\ndropped dup\n")
        new = write(self.repo / "CLAUDE.md", "# Title\nrule one\nrule two\n")
        write(self.repo / "notes" / "history.md", "history line\n")
        out = self.run_cw("verify-split", str(old), str(new), str(self.repo / "notes"), code=1)
        self.assertIn("1 missing", out)
        self.assertIn("dropped dup", out)
        allow = write(self.repo / "allow.txt", "dropped dup\n")
        self.run_cw("verify-split", str(old), str(new), str(self.repo / "notes"), "--allow", str(allow))

    def test_verify_split_counts_repeated_lines(self):
        old = write(self.repo / "old.md", "## Backend\n- Run tests: make check\n## Frontend\n- Run tests: make check\n")
        new = write(self.repo / "AGENTS.md", "## Backend\n- Run tests: make check\n## Frontend\n")
        res = self.cw_json("verify-split", str(old), str(new), code=1)
        self.assertEqual(res["missing"], ["- Run tests: make check"])

    def test_verify_split_keeps_short_lines_with_words(self):
        old = write(self.repo / "old.md", "Deploy on Fridays?\nNo.\n---\n|---|\n")
        new = write(self.repo / "AGENTS.md", "Deploy on Fridays?\n")
        res = self.cw_json("verify-split", str(old), str(new), code=1)
        self.assertEqual(res["missing"], ["No."])  # rules and table separators carry no words

    def test_verify_split_keeps_indentation_inside_code_blocks(self):
        old = write(self.repo / "old.md", "```yaml\nenv:\n  prod: blue\n  dev: green\n```\n")
        new = write(self.repo / "AGENTS.md", "```yaml\nenv:\nprod: blue\ndev: green\n```\n")
        res = self.cw_json("verify-split", str(old), str(new), code=1)
        self.assertEqual(sorted(res["missing"]), ["  dev: green", "  prod: blue"])
        write(self.repo / "AGENTS.md", "~~~yaml\nenv:\n  prod:   blue\n  dev: green\n~~~\n")  # inner spacing may change
        self.run_cw("verify-split", str(old), str(new))

    def test_verify_split_ignores_the_old_file_inside_a_new_directory(self):
        old = write(self.repo / "notes" / "CLAUDE.md.bak", "only in the backup\n")
        write(self.repo / "notes" / "CLAUDE.md.bak.md", "only in the backup\n")  # a copy that does count
        self.run_cw("verify-split", str(old), str(self.repo / "notes"))
        (self.repo / "notes" / "CLAUDE.md.bak.md").unlink()
        old_md = write(self.repo / "notes" / "old.md", "kept nowhere else\n")
        self.run_cw("verify-split", str(old_md), str(self.repo / "notes"), code=1)

    def test_verify_split_unreadable_inputs_exit_2(self):
        new = write(self.repo / "AGENTS.md", "x\n")
        self.run_cw("verify-split", str(self.repo / "missing.md"), str(new), code=2)
        old = write(self.repo / "old.md", "rule\n")
        self.run_cw("verify-split", str(old), str(self.repo / "nope"), code=2)
        self.run_cw("verify-split", str(old), str(new), "--allow", str(self.repo / "nope.txt"), code=2)

    # ------------------------------------------------------------------ gate and paths
    def test_missing_or_file_path_exits_2(self):
        for cmd in ("files", "skills", "report"):
            self.run_cw(cmd, str(self.repo / "no-such-dir"), code=2)
        f = write(self.repo / "CLAUDE.md", "x")
        self.run_cw("files", str(f), "--max-tokens", "1", code=2)

    def test_max_tokens_gate_counts_text_codex_cuts_off(self):
        write(self.repo / "AGENTS.md", "a" * 100 * 1024)  # ~34k tokens, Codex keeps 32 KiB of it
        p = subprocess.run([sys.executable, str(CW), "files", str(self.repo), "--harness", "codex", "--max-tokens", "15000"],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("never sees", p.stderr)

    # ------------------------------------------------------------------ robustness
    def test_bom_does_not_hide_frontmatter(self):
        write(self.repo / ".claude" / "rules" / "scoped.md", "﻿---\npaths:\n  - src/**\n---\nonly src")
        write(self.home / ".claude" / "skills" / "dir-name" / "SKILL.md", "﻿---\nname: real-name\ndescription: hi\n---\n")
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual(r["files"], [])
        s = self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual([h["name"] for h in s["heaviest"]], ["real-name"])

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions")
    def test_unreadable_folder_does_not_crash(self):
        locked = Path(self.tmp.name) / "locked"
        write(locked / "codex" / "AGENTS.md", "x")
        self.env["CODEX_HOME"] = str(locked / "codex")
        self.env["CLAUDE_CONFIG_DIR"] = str(locked / "claude")
        locked.chmod(0)
        try:
            self.cw_json("files", str(self.repo))
            self.cw_json("skills", str(self.repo))
            self.cw_json("sessions")
        finally:
            locked.chmod(0o755)

    def test_malformed_log_lines_are_skipped(self):
        good = {"type": "assistant", "cwd": str(self.repo), "timestamp": "2026-10-01T00:00:00Z",
                "message": {"id": "ok", "usage": {"input_tokens": 1_000}}}
        bad = ["[]", '"assistant"', "42", "null",
               json.dumps({"type": "assistant", "message": []}),
               json.dumps({"type": "assistant", "message": {"id": "s", "usage": {"input_tokens": "lots"}}}),
               json.dumps({"type": "assistant", "message": {"id": "u", "usage": []}}),
               '{"type": "assistant", "cut off']
        write(self.home / ".claude" / "projects" / "-repo" / "x.jsonl", "\n".join(bad + [json.dumps(good)]) + "\n")
        meta = {"type": "session_meta", "payload": {"cwd": str(self.repo)}}
        tok = {"type": "event_msg", "timestamp": "2026-10-01T00:00:00Z", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 5}, "last_token_usage": {"input_tokens": 2_000}}}}
        cbad = ['["token_count"]', '{"type": "session_meta", "payload": []}',
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": []}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                                                              "info": {"last_token_usage": {"input_tokens": None}}}})]
        write(self.home / ".codex" / "sessions" / "2026" / "10" / "01" / "rollout-x.jsonl",
              "\n".join([json.dumps(meta)] + cbad + [json.dumps(tok)]) + "\n")
        res = self.cw_json("sessions")
        self.assertEqual(res[f"claude {self.repo.resolve()}"]["requests"], 1)
        self.assertEqual(res[f"codex {self.repo.resolve()}"]["requests"], 1)

    def test_codex_guardian_and_spawned_threads_count_as_subagents(self):
        def rollout(name, thread_source, source):
            payload = {"cwd": str(self.repo), "source": source}
            if thread_source is not None:
                payload["thread_source"] = thread_source
            jsonl(self.home / ".codex" / "sessions" / "2026" / "11" / "01" / f"rollout-{name}.jsonl", [
                {"type": "session_meta", "timestamp": "2026-11-01T00:00:00Z", "payload": payload},
                {"type": "event_msg", "timestamp": "2026-11-01T00:00:00Z", "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"total_tokens": 1}, "last_token_usage": {"input_tokens": 10_000}}}}])
        rollout("user-cli", "user", "cli")
        rollout("old-desktop", None, "vscode")
        rollout("handoff", "chatgpt_handoff", "vscode")
        rollout("guardian", "guardian_review", {"subagent": {"other": "guardian"}})
        rollout("spawned", "subagent", {"subagent": {"thread_spawn": {"depth": 1}}})
        rollout("guardian-new-label", "reviewer_v2", {"subagent": {"other": "guardian"}})
        key = f"codex {self.repo.resolve()}"
        self.assertEqual(self.cw_json("sessions", "--harness", "codex")[key]["sessions"], 3)
        self.assertEqual(self.cw_json("sessions", "--harness", "codex", "--include-subagents")[key]["sessions"], 6)

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_symlinked_rules_folder_is_counted_once(self):
        shared = write(Path(self.tmp.name) / "shared-rules" / "style.md", "shared rule")
        (self.repo / ".claude" / "rules").mkdir(parents=True)
        os.symlink(shared.parent, self.repo / ".claude" / "rules" / "shared", target_is_directory=True)
        os.symlink(self.repo / ".claude" / "rules", self.repo / ".claude" / "rules" / "loop", target_is_directory=True)
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual([Path(f["path"]).name for f in r["files"]], ["style.md"])

    def test_gemini_stops_at_the_git_root(self):
        write(self.repo.parent / "GEMINI.md", "outside the repo")
        write(self.repo / "GEMINI.md", "inside")
        r = self.cw_json("files", str(self.repo), "--harness", "gemini")["gemini"]
        self.assertEqual([f["role"] for f in r["files"]], ["project"])

    def test_window_fills_in_for_codex_only_without_logs(self):
        write(self.home / ".agents" / "skills" / "one" / "SKILL.md", "---\nname: one\ndescription: hi\n---\n")
        s = self.cw_json("skills", str(self.repo), "--harness", "codex", "--window", "1000000")
        self.assertEqual((s["codex"]["budget"], s["codex"]["window_source"]), (20_000, "--window"))

    def test_skill_budget_uses_codex_window_from_logs(self):
        write(self.home / ".agents" / "skills" / "one" / "SKILL.md", "---\nname: one\ndescription: hi\n---\n")
        jsonl(self.home / ".codex" / "sessions" / "2026" / "11" / "01" / "rollout-w.jsonl", [
            {"type": "event_msg", "timestamp": "2026-11-01T00:00:00Z", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": 1}, "last_token_usage": {"input_tokens": 1},
                "model_context_window": 400_000}}}])
        s = self.cw_json("skills", str(self.repo), "--harness", "claude,codex")
        self.assertEqual(s["codex"]["budget"], 8_000)              # 2% of the window Codex reported
        self.assertEqual(s["claude"]["window_source"], "default")  # no window in Claude logs: assumed
        s = self.cw_json("skills", str(self.repo), "--harness", "claude,codex", "--window", "1000000")
        # Claude Code budgets the listing in characters: 1% of the window at ~4 chars per token
        self.assertEqual((s["claude"]["budget"], s["claude"]["budget_unit"]), (40_000, "chars"))
        # Codex records its own window; --window must not override it (it once did: 20,000)
        self.assertEqual((s["codex"]["budget"], s["codex"]["budget_unit"]), (8_000, "tokens"))
        self.assertEqual(s["codex"]["window_source"], "codex logs")
        write(self.home / ".claude" / "settings.json", json.dumps({"skillListingBudgetFraction": 0.02}))
        self.assertEqual(self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]["budget"], 16_000)
        self.env["SLASH_COMMAND_TOOL_CHAR_BUDGET"] = "123456"
        s = self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual((s["budget"], s["basis"]), (123_456, "SLASH_COMMAND_TOOL_CHAR_BUDGET"))

    # ------------------------------------------------------------------ installer
    def run_install(self, *args, code=0):
        p = subprocess.run([sys.executable, str(CW.parents[3] / "install.py"), *args],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, code, p.stdout + p.stderr)
        return p.stdout

    def test_install_links_every_harness_and_uninstalls(self):
        self.run_install()
        for d in (self.home / ".agents" / "skills", self.home / ".claude" / "skills"):
            self.assertTrue((d / "context-weight" / "SKILL.md").is_file(), d)
        self.run_install()  # idempotent
        self.run_install("--uninstall")
        self.assertFalse((self.home / ".claude" / "skills" / "context-weight").exists())

    def test_install_into_one_repo_copies(self):
        self.run_install("--project", str(self.repo))
        for d in (".agents/skills", ".claude/skills"):
            sk = self.repo / d / "context-weight"
            self.assertTrue((sk / "scripts" / "cw.py").is_file())
            self.assertFalse(sk.is_symlink())  # a copy, so it can be committed with the repo

    def test_install_never_replaces_a_real_folder(self):
        write(self.home / ".claude" / "skills" / "context-weight" / "SKILL.md", "user's own")
        out = self.run_install()
        self.assertIn("skip", out)
        self.assertEqual((self.home / ".claude" / "skills" / "context-weight" / "SKILL.md").read_text(), "user's own")

    def test_install_keeps_a_user_copy_even_with_the_same_skill_name(self):
        mine = self.home / ".claude" / "skills" / "context-weight"
        write(mine / "SKILL.md", "---\nname: context-weight\n---\nmy edited copy")
        write(mine / "notes.md", "my notes")
        self.assertIn("skip", self.run_install())
        self.run_install("--uninstall")
        self.assertEqual((mine / "notes.md").read_text(), "my notes")

    def test_install_replaces_and_removes_only_its_own_copy(self):
        self.run_install("--copy")
        dest = self.home / ".agents" / "skills" / "context-weight"
        write(dest / "stale.txt", "from an older copy")
        self.assertIn("copy", self.run_install("--copy"))
        self.assertFalse((dest / "stale.txt").exists())
        self.run_install("--uninstall")
        self.assertFalse(dest.exists())

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_install_never_deletes_its_own_source(self):
        import shutil
        checkout = Path(self.tmp.name) / "checkout"
        shutil.copytree(CW.parents[3] / "skills", checkout / "skills")
        shutil.copy(CW.parents[3] / "install.py", checkout / "install.py")
        (self.home / ".claude").mkdir(parents=True)
        os.symlink(checkout / "skills", self.home / ".claude" / "skills", target_is_directory=True)
        for args in ((), ("--copy",), ("--uninstall",)):
            subprocess.run([sys.executable, str(checkout / "install.py"), *args], env=self.env, capture_output=True)
            src = checkout / "skills" / "context-weight"
            self.assertFalse(src.is_symlink(), args)
            self.assertTrue((src / "scripts" / "cw.py").is_file(), args)

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_install_reports_a_broken_folder_and_still_installs_the_rest(self):
        (self.home / ".agents").mkdir(parents=True)
        os.symlink(self.home / "gone", self.home / ".agents" / "skills")  # dangling
        p = subprocess.run([sys.executable, str(CW.parents[3] / "install.py")], env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertTrue((self.home / ".claude" / "skills" / "context-weight" / "SKILL.md").is_file())

    # ------------------------------------------------------------------ more inputs
    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_verify_split_reads_each_real_file_once(self):
        old = write(self.repo / "old.md", "- Run tests: make check\n- Run tests: make check\n")
        write(self.repo / "AGENTS.md", "- Run tests: make check\n")
        os.symlink(self.repo / "AGENTS.md", self.repo / "CLAUDE.md")
        res = self.cw_json("verify-split", str(old), str(self.repo / "AGENTS.md"), str(self.repo / "CLAUDE.md"), code=1)
        self.assertEqual(res["missing_copies"], {"- Run tests: make check": [1, 2]})
        self.cw_json("verify-split", str(old), str(self.repo / "AGENTS.md"), str(self.repo), code=1)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions")
    def test_unreadable_instruction_file_blocks_the_gate(self):
        f = write(self.repo / "AGENTS.md", "a" * 100 * 1024)
        f.chmod(0)
        try:
            p = subprocess.run([sys.executable, str(CW), "files", str(self.repo), "--max-tokens", "6000"],
                               env=self.env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("cannot read", p.stderr)
            self.run_cw("files", str(self.repo))                     # without the gate: a warning, exit 0
            self.assertIn("cannot read", self.run_cw("report", str(self.repo)))
        finally:
            f.chmod(0o644)

    def test_bad_arguments_exit_2(self):
        self.run_cw("sessions", "--since", "2026-13-01", code=2)
        self.run_cw("sessions", "--compare-at", "yesterday", code=2)
        self.run_cw("files", str(self.repo), "--harness", ",", code=2)
        self.run_cw("files", str(self.repo), "--harness", "cursor", code=2)
        self.run_cw("files", str(self.repo), "--max-tokens", "-1", code=2)

    def test_skill_description_cap_follows_settings(self):
        write(self.home / ".claude" / "skills" / "big" / "SKILL.md", "---\nname: big\ndescription: " + "d" * 3_000 + "\n---\n")
        write(self.home / ".claude" / "settings.json", json.dumps({"skillListingMaxDescChars": 2_000}))
        self.assertEqual(self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]["desc_chars"], 2_000)

    def test_odd_config_shapes_do_not_crash(self):
        write(self.home / ".claude" / "settings.json", json.dumps({"enabledPlugins": ["not", "a", "dict"],
                                                                   "skillListingBudgetFraction": "lots"}))
        write(self.home / ".claude" / "plugins" / "installed_plugins.json", json.dumps({"plugins": {"x@m": "nope"}}))
        write(self.home / ".config" / "opencode" / "opencode.json", json.dumps({"instructions": "notes.md"}))
        write(self.repo / "opencode.json", json.dumps(["a", "list"]))
        write(self.home / ".gemini" / "settings.json", json.dumps(["a", "list"]))
        write(self.home / ".codex" / "config.toml", "project_doc_max_bytes = 'big'\n")
        self.cw_json("report", str(self.repo))


class ManifestTest(unittest.TestCase):
    """The plugin manifests that `/plugin install` and `codex plugin add` read."""

    ROOT = CW.parents[3]
    FILES = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/marketplace.json")

    def load(self, rel):
        return json.loads((self.ROOT / rel).read_text(encoding="utf-8"))

    def test_manifests_agree_with_the_script(self):
        out = subprocess.run([sys.executable, str(CW), "--version"], capture_output=True, text=True)
        version = out.stdout.strip()
        self.assertTrue(version)
        for rel in self.FILES[:2]:
            plugin = self.load(rel)
            self.assertEqual(plugin["name"], "context-weight", rel)
            self.assertEqual(plugin["version"], version, rel)
            self.assertTrue((self.ROOT / plugin["skills"] / "context-weight" / "SKILL.md").is_file(), rel)
        market = self.load(self.FILES[2])
        self.assertEqual(market["name"], "context-weight")
        [entry] = market["plugins"]
        self.assertEqual(entry["name"], "context-weight")
        self.assertTrue((self.ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file())

    def test_manifests_carry_no_email(self):
        email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
        for rel in self.FILES:
            self.assertIsNone(email.search((self.ROOT / rel).read_text(encoding="utf-8")), rel)


if __name__ == "__main__":
    unittest.main()
