"""Tests for cw.py on a synthetic HOME. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CW = Path(__file__).resolve().parents[1] / "skills" / "context-weight" / "scripts" / "cw.py"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def jsonl(path: Path, records) -> None:
    write(path, "".join(json.dumps(r) + "\n" for r in records))


class CwTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home = t / "home"
        self.repo = t / "work" / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DISABLE_CLAUDE_CODE")}
        self.env["HOME"] = str(self.home)

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


if __name__ == "__main__":
    unittest.main()
