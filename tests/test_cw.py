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
                    if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DISABLE_CLAUDE_CODE",
                                 "SLASH_COMMAND_TOOL_CHAR_BUDGET")}
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
        self.assertEqual((s["codex"]["budget"], s["codex"]["budget_unit"]), (20_000, "tokens"))
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


if __name__ == "__main__":
    unittest.main()
