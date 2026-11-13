"""Tests for cw.py on a synthetic HOME. Run: python3 -m unittest discover -s tests -v"""
import datetime as dt
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
from unittest import mock

CW = Path(__file__).resolve().parents[1] / "skills" / "context-weight" / "scripts" / "cw.py"


def write(path: Path, text: str, exact=False) -> Path:
    """Write `text` as a text file: on Windows each \\n becomes \\r\\n, unless `exact` asks for
    these bytes, as a test that counts bytes or tokens needs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if exact:
        path.write_bytes(text.encode("utf-8"))
    else:
        path.write_text(text, encoding="utf-8")
    return path


def rules(tag: str, n: int) -> str:
    """`n` distinct lines of about 100 bytes, so a cut removes real lines, as in a real file."""
    return "".join(f"{tag} {i:05d} " + "x" * 90 + "\n" for i in range(n))


def jsonl(path: Path, records) -> None:
    write(path, "".join(json.dumps(r) + "\n" for r in records))


def load_cw():
    """cw.py as a module, for the few checks that need one function rather than the command line."""
    spec = importlib.util.spec_from_file_location("cw_under_test", str(CW))
    cw = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cw)
    return cw


class CwTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.tmp_path = t
        self.home = t / "home"
        self.repo = t / "work" / "repo"
        (self.repo / ".git").mkdir(parents=True)
        # Nothing of the caller's setup may leak in. A git hook exports GIT_DIR and GIT_INDEX_FILE,
        # which would point the fixtures' git commands at the caller's own repository, and a global
        # git ignore file under XDG_CONFIG_HOME can hide the fixtures' files.
        drop = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "OPENCODE_DISABLE_CLAUDE_CODE", "SLASH_COMMAND_TOOL_CHAR_BUDGET",
                "BROWSER", "XDG_CONFIG_HOME", "ANTHROPIC_MODEL", "CLAUDE_CODE_DISABLE_1M_CONTEXT", "DISABLE_COMPACT",
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS", "CLAUDE_CODE_ORGANIZATION_UUID", "CLAUDE_CODE_ACCOUNT_UUID",
                "CLAUDE_CODE_DISABLE_POLICY_SKILLS", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_PROJECT_DIR_NAME")
        self.env = {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("GIT_")}
        self.env["HOME"] = str(self.home)
        self.env["USERPROFILE"] = str(self.home)  # Windows: expanduser reads USERPROFILE, not HOME
        self.env["XDG_CONFIG_HOME"] = str(self.home / ".config")
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["PYTHONIOENCODING"] = "utf-8"

    def tearDown(self):
        self.tmp.cleanup()

    def mine(self, entries):
        """Entries inside this test's folder. Claude Code also loads CLAUDE.md files above the
        working folder, so the developer's own files above the temp folder must not count."""
        top = str(self.tmp_path.resolve()) + os.sep
        return [e for e in entries if str(Path(e["path"]).resolve()).startswith(top)]

    def run_cw(self, *args, code=0):
        # The child writes UTF-8 (PYTHONIOENCODING); decode it so, not in the locale's code page.
        p = subprocess.run([sys.executable, str(CW), *args], env=self.env, capture_output=True,
                           text=True, encoding="utf-8")
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
        files = self.mine(r["files"])
        names = [Path(f["path"]).name for f in files]
        self.assertEqual(names, ["CLAUDE.md", "extra.md", "CLAUDE.md", "big.md", "eager.md"])
        self.assertNotIn("not.md", names)
        self.assertEqual(files[3]["role"], "project import")
        self.assertEqual(files[3]["tokens"], 10_000)          # 30k ASCII chars / 3
        self.assertTrue(any("imports load at launch" in w for w in r["warnings"]))
        self.assertTrue(any("1 path-scoped" in n for n in r["notes"]))
        self.assertEqual(r["repo_tokens"], sum(f["tokens"] for f in files[2:]))

    def test_thai_estimate_counts_one_token_per_char(self):
        write(self.repo / "CLAUDE.md", "ก" * 900 + "a" * 300)
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual(self.mine(r["files"])[0]["tokens"], 1_000)

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

    def test_codex_config_reads_the_same_without_tomllib(self):
        # Python 3.8 to 3.10 have no tomllib. The fallback once read `65_536` as 65 and took a key
        # from a [profiles.x] table as the global one, so the gate's verdict hung on the Python.
        cw = load_cw()
        config = self.home / ".codex" / "config.toml"
        cases = [("project_doc_max_bytes = 65_536\n", 65_536, []),
                 ("project_doc_max_bytes = 0x1_0000  # 64 KiB\n", 65_536, []),
                 ("project_doc_max_bytes = 'big'\n", 32_768, []),
                 ("project_doc_fallback_filenames = [\"TEAM.md\", 'NOTES.md']\n", 32_768, ["TEAM.md", "NOTES.md"]),
                 ('model = "o3"\n\n[profiles.big]\nproject_doc_max_bytes = 1_048_576\n'
                  'project_doc_fallback_filenames = ["TEAM.md"]\n', 32_768, [])]
        for text, max_bytes, names in cases:
            write(config, text)
            for modules in ({"tomllib": None}, {}):  # None makes `import tomllib` fail, as on 3.8
                with mock.patch.dict(os.environ, {"CODEX_HOME": str(config.parent)}), \
                        mock.patch.dict(sys.modules, modules):
                    self.assertEqual(cw._codex_config(), (max_bytes, names), (text, modules))

    def test_codex_fallback_filenames(self):
        write(self.home / ".codex" / "config.toml", 'project_doc_fallback_filenames = ["TEAM.md"]\n')
        write(self.repo / "TEAM.md", "team rules")
        pkg = self.repo / "pkg"
        write(pkg / "AGENTS.md", "pkg rules")
        write(pkg / "TEAM.md", "not read: AGENTS.md comes first")
        r = self.cw_json("files", str(pkg), "--harness", "codex")["codex"]
        self.assertEqual([Path(f["path"]).relative_to(self.repo.resolve()).as_posix() for f in r["files"]],
                         ["TEAM.md", "pkg/AGENTS.md"])

    def test_codex_counts_only_the_part_it_keeps(self):
        write(self.repo / "AGENTS.md", "a" * 40 * 1024)
        f = self.cw_json("files", str(self.repo), "--harness", "codex")["codex"]["files"][0]
        self.assertEqual((f["tokens"], f["file_tokens"]), (round(32 * 1024 / 3), round(40 * 1024 / 3)))

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
        # Claude Code loads AGENTS.md where no CLAUDE.md is, unless set to read CLAUDE.md only.
        write(self.home / ".claude" / "settings.json", json.dumps(
            {"pluginConfigs": {"cc-plugin-agents-md@builtin": {"options": {"instructionFiles": "claude-md"}}}}))
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        self.assertTrue(res.get("cross"), "AGENTS.md that only Codex reads went unflagged")
        (self.repo / "AGENTS.md").unlink()
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        self.assertFalse(res.get("cross"))

    def test_cross_check_leaves_out_claude_files_above_the_repo(self):
        # A CLAUDE.md in a parent folder loads for Claude Code alone, in every repo below it. It is
        # listed, but this repo holds no rules, so its harnesses do not follow different rules here.
        parent = self.repo.parent / "CLAUDE.md"
        write(parent, "Use the shared lint config.\n" * 400)
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        self.assertIn(parent.resolve(), [Path(e["path"]).resolve() for e in res["claude"]["files"]])
        self.assertFalse(res.get("cross"))
        write(self.repo / "CLAUDE.md", "c" * 30_000)  # rules in the repo itself still count
        self.assertTrue(self.cw_json("files", str(self.repo), "--harness", "claude,codex").get("cross"))

    def test_gemini_context_filenames(self):
        write(self.home / ".gemini" / "settings.json", json.dumps({"context": {"fileName": ["AGENTS.md", "GEMINI.md"]}}))
        write(self.repo / "AGENTS.md", "agents")
        write(self.repo / "GEMINI.md", "gemini")
        r = self.cw_json("files", str(self.repo), "--harness", "gemini")["gemini"]
        self.assertEqual(sorted(Path(f["path"]).name for f in r["files"]), ["AGENTS.md", "GEMINI.md"])

    def test_gemini_file_names_of_the_wrong_type_do_not_crash(self):
        write(self.repo / "AGENTS.md", "agents")
        write(self.repo / "GEMINI.md", "gemini")
        for value, names in ((123, ["GEMINI.md"]), (True, ["GEMINI.md"]), ({"a": 1}, ["GEMINI.md"]),
                             ([None], ["GEMINI.md"]), ([1, "AGENTS.md"], ["AGENTS.md"])):
            write(self.home / ".gemini" / "settings.json", json.dumps({"context": {"fileName": value}}))
            r = self.cw_json("files", str(self.repo), "--harness", "gemini")["gemini"]
            self.assertEqual([Path(f["path"]).name for f in r["files"]], names, value)

    def test_claude_imports_stop_after_four_hops(self):
        write(self.repo / "CLAUDE.md", "@a.md\n")
        for name, nxt in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"), ("e", "f")):
            write(self.repo / f"{name}.md", f"@{nxt}.md\n")
        write(self.repo / "f.md", "end")
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual([Path(f["path"]).name for f in self.mine(r["files"])],
                         ["CLAUDE.md", "a.md", "b.md", "c.md", "d.md"])

    def test_claude_reads_claude_md_inside_dot_claude(self):
        write(self.repo / ".claude" / "CLAUDE.md", "rules kept under .claude\n")
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual([Path(f["path"]).relative_to(self.repo.resolve()).as_posix() for f in self.mine(r["files"])],
                         [".claude/CLAUDE.md"])

    # ------------------------------------------------------------------ what Claude Code loads
    _CLAUDE_NAMES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")

    def _claude_md_above(self):
        """A CLAUDE.md this machine keeps above the temp folder, if any. Claude Code reads
        AGENTS.md only where no CLAUDE.md is in the start folder or above it."""
        for d in self.tmp_path.resolve().parents:
            if d == Path(d.anchor):
                continue  # Claude Code does not read the filesystem root
            for name in self._CLAUDE_NAMES:
                try:
                    if (d / name).is_file() and (d / name).read_text(encoding="utf-8", errors="replace").strip():
                        return d / name
                except OSError:
                    pass
        return None

    def _skip_if_claude_md_above(self):
        above = self._claude_md_above()
        if above:
            self.skipTest(f"{above} keeps AGENTS.md out of Claude Code on this machine")

    def _claude_rel(self, files):
        """(path under the test's temp folder, role) for each Claude Code entry in it."""
        top = self.tmp_path.resolve()
        return [(Path(f["path"]).resolve().relative_to(top).as_posix(), f["role"]) for f in self.mine(files)]

    def _claude_loads(self, path):
        return [rel for rel, _ in self._claude_rel(self.cw_json("files", str(path), "--harness", "claude")["claude"]["files"])]

    def test_claude_reads_agents_md_when_no_claude_md(self):
        # Claude Code 2.1.277+ loads AGENTS.md where no CLAUDE.md is: every AGENTS.md and
        # .claude/AGENTS.md from the start folder up, with its @imports. The user's own
        # ~/.claude/CLAUDE.md is not such a file; AGENTS.local.md, .agents/ and a subfolder's
        # AGENTS.md do not load at startup.
        self._skip_if_claude_md_above()
        write(self.home / ".claude" / "CLAUDE.md", "my own rules\n")
        write(self.repo / "AGENTS.md", rules("shared", 50) + "@docs/more.md\n")
        write(self.repo / "docs" / "more.md", "more rules\n")
        write(self.repo / ".claude" / "AGENTS.md", "kept under .claude\n")
        write(self.repo.parent / "AGENTS.md", "workspace rules\n")
        for name in ("AGENTS.local.md", ".agents/AGENTS.md", "sub/AGENTS.md"):
            write(self.repo / name, "not read by Claude Code at startup\n")
        res = self.cw_json("files", str(self.repo), "--harness", "claude,codex")
        r = res["claude"]
        self.assertEqual(self._claude_rel(r["files"]), [
            ("home/.claude/CLAUDE.md", "user"),
            ("work/AGENTS.md", "project"),
            ("work/repo/AGENTS.md", "project"),
            ("work/repo/docs/more.md", "project import"),
            ("work/repo/.claude/AGENTS.md", "project"),
        ])
        self.assertTrue(any("AGENTS.md" in n and "no CLAUDE.md" in n for n in r["notes"]), r["notes"])
        in_repo = [f for f, (rel, _) in zip(self.mine(r["files"]), self._claude_rel(r["files"]))
                   if rel.startswith("work/repo/")]
        self.assertEqual(r["repo_tokens"], sum(f["tokens"] for f in in_repo))
        self.assertFalse(res.get("cross"))  # both agents read this AGENTS.md

    def test_any_claude_md_up_the_tree_keeps_agents_md_out(self):
        self._skip_if_claude_md_above()
        write(self.repo / "AGENTS.md", "agents rules\n")
        start = self.repo / "pkg"
        start.mkdir()
        for spot in ("work/repo/pkg/CLAUDE.md", "work/repo/.claude/CLAUDE.md", "work/repo/CLAUDE.local.md",
                     "work/CLAUDE.md", "work/.claude/CLAUDE.md"):
            p = write(self.tmp_path / spot, "claude rules\n")
            self.assertEqual(self._claude_loads(start), [spot], spot)
            p.unlink()
        # A CLAUDE.md that holds nothing loads nothing, so it does not count.
        write(self.repo / "CLAUDE.md", " \n\n")
        self.assertEqual(self._claude_loads(start), ["work/repo/AGENTS.md"])
        # The user's own CLAUDE.md does not count either, also for a repo inside the home folder,
        # where it sits at the home folder's .claude/CLAUDE.md.
        repo2 = self.home / "code" / "r2"
        (repo2 / ".git").mkdir(parents=True)
        write(repo2 / "AGENTS.md", "agents rules\n")
        write(self.home / ".claude" / "CLAUDE.md", "my own rules\n")
        self.assertEqual(self._claude_loads(repo2), ["home/.claude/CLAUDE.md", "home/code/r2/AGENTS.md"])

    def _plugin_options(self, options, plugin="cc-plugin-agents-md@builtin", where=None):
        settings = {"pluginConfigs": {plugin: {"options": options}}}
        write(where or self.home / ".claude" / "settings.json", json.dumps(settings))

    def test_instruction_files_setting_picks_what_claude_code_loads(self):
        # Claude Code's built-in agents-md plugin reads its instructionFiles option (and the older
        # projectInstructions) from user settings only, under either plugin id; /plugin can turn
        # the plugin off in any settings file, and then AGENTS.md never loads.
        self._skip_if_claude_md_above()
        write(self.home / ".claude" / "CLAUDE.md", "my own rules\n")
        write(self.repo / "CLAUDE.md", "claude rules\n")
        write(self.repo / "AGENTS.md", "agents rules\n")
        write(self.repo / ".claude" / "rules" / "r.md", "a rule\n")
        slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(self.repo.resolve()))
        write(self.home / ".claude" / "projects" / slug / "memory" / "MEMORY.md", "remember this\n")
        U, C, A, R = "home/.claude/CLAUDE.md", "work/repo/CLAUDE.md", "work/repo/AGENTS.md", "work/repo/.claude/rules/r.md"
        M = f"home/.claude/projects/{slug}/memory/MEMORY.md"
        modes = {"default": ([U, C, R, M], [U, R, A, M]), "claude-md": ([U, C, R, M], [U, R, M]),
                 "and": ([U, C, R, A, M], [U, R, A, M]), "managed-only": ([M], [M])}
        cases = [({}, "default"),
                 ({"instructionFiles": "claude-md-or-agents-md"}, "default"),
                 ({"instructionFiles": "claude-md"}, "claude-md"),
                 ({"instructionFiles": "claude-md-and-agents-md"}, "and"),
                 ({"instructionFiles": "managed-only"}, "managed-only"),
                 ({"instructionFiles": "bogus"}, "default"),
                 ({"projectInstructions": "none"}, "managed-only"),
                 ({"projectInstructions": "claude"}, "claude-md"),
                 ({"projectInstructions": "agents-fallback"}, "default"),
                 ({"projectInstructions": "both"}, "and"),
                 ({"projectInstructions": "bogus"}, "claude-md"),
                 ({"projectInstructions": 7}, "claude-md"),
                 ({"projectInstructions": None}, "claude-md"),
                 # projectInstructions counts only while instructionFiles is the default
                 ({"instructionFiles": "claude-md-and-agents-md", "projectInstructions": "none"}, "and"),
                 ({"instructionFiles": "claude-md-or-agents-md", "projectInstructions": "both"}, "and")]
        for options, mode in cases:
            for plugin in ("cc-plugin-agents-md@builtin", "agents-md@builtin"):
                self._plugin_options(options, plugin)
                for with_claude_md, expect in zip((True, False), modes[mode]):
                    if not with_claude_md:
                        os.replace(self.repo / "CLAUDE.md", self.tmp_path / "parked.md")
                    try:
                        self.assertEqual(self._claude_loads(self.repo), expect, (options, plugin, with_claude_md))
                    finally:
                        if not with_claude_md:
                            os.replace(self.tmp_path / "parked.md", self.repo / "CLAUDE.md")
        # Project and local settings do not set the option.
        write(self.home / ".claude" / "settings.json", "{}")
        for name in ("settings.json", "settings.local.json"):
            self._plugin_options({"instructionFiles": "managed-only"}, where=self.repo / ".claude" / name)
            self.assertEqual(self._claude_loads(self.repo), modes["default"][0], name)
            (self.repo / ".claude" / name).unlink()
        # Turned off through enabledPlugins: the highest settings file that names it decides.
        (self.repo / "CLAUDE.md").unlink()
        off = [U, R, M]
        for files, expect in (({"user": {"cc-plugin-agents-md@builtin": False}}, off),
                              ({"project": {"agents-md@builtin": False}}, off),
                              ({"user": {"cc-plugin-agents-md@builtin": False},
                                "local": {"cc-plugin-agents-md@builtin": True}}, modes["default"][1]),
                              ({"user": {"cc-plugin-agents-md@builtin": True}}, modes["default"][1])):
            paths = {"user": self.home / ".claude" / "settings.json",
                     "project": self.repo / ".claude" / "settings.json",
                     "local": self.repo / ".claude" / "settings.local.json"}
            for scope, value in files.items():
                write(paths[scope], json.dumps({"enabledPlugins": value}))
            self.assertEqual(self._claude_loads(self.repo), expect, files)
            for p in paths.values():
                if p.exists():
                    p.unlink()

    def test_agents_md_beside_claude_md_loads_once(self):
        # claude-md-and-agents-md skips an AGENTS.md that CLAUDE.md already imports or links to,
        # and one whose text is the same as a loaded project file.
        self._plugin_options({"instructionFiles": "claude-md-and-agents-md"})
        write(self.repo / "AGENTS.md", "agents rules\n")
        for claude_md in ("@AGENTS.md\n", "agents rules\n\n"):
            write(self.repo / "CLAUDE.md", claude_md)
            names = [Path(rel).name for rel in self._claude_loads(self.repo)]
            self.assertEqual(sorted(names), sorted({"@AGENTS.md\n": ["CLAUDE.md", "AGENTS.md"],
                                                    "agents rules\n\n": ["CLAUDE.md"]}[claude_md]), claude_md)
        if os.name != "nt":
            (self.repo / "CLAUDE.md").unlink()
            (self.repo / "CLAUDE.md").symlink_to("AGENTS.md")
            self.assertEqual(len(self._claude_loads(self.repo)), 1)

    def test_before_and_after_weighs_agents_md_as_claude_code_loaded_it(self):
        self._skip_if_claude_md_above()
        self._git_repo()
        write(self.repo / ".gitignore", "CLAUDE.local.md\n")
        write(self.repo / "AGENTS.md", rules("rule", 300))  # 30,600 bytes, under Codex's 32 KiB
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "AGENTS.md", "short\n")
        slim = self._commit("slim", "2026-02-01T00:00:00+00:00")
        args = ("report", str(self.repo), "--days", "40000", "--harness", "claude,codex")
        cut = {"before": 10_200, "after": 2, "now": 2}
        # No CLAUDE.md anywhere: Claude Code loaded the old AGENTS.md too, though the old commit is
        # weighed in a temp folder that here has a CLAUDE.md above it.
        under = self.tmp_path / "under"
        write(under / "CLAUDE.md", "not on this repo's path\n")
        (under / "tmp").mkdir()
        saved = dict(self.env)
        self.env.update(TMPDIR=str(under / "tmp"), TEMP=str(under / "tmp"), TMP=str(under / "tmp"))
        try:
            c = self.cw_json(*args)["compare"]
        finally:
            self.env = saved
        self.assertEqual((c["commit"], c["files"]), (slim, {"claude": cut, "codex": cut}))
        # A CLAUDE.md above the repo, or a CLAUDE.local.md git ignores, kept AGENTS.md out for
        # Claude Code then as it does now, though neither is in the old commit.
        for spot in (self.repo.parent / "CLAUDE.md", self.repo / "CLAUDE.local.md"):
            write(spot, "claude rules\n")
            c = self.cw_json(*args)["compare"]
            self.assertEqual((c["commit"], c["files"]), (slim, {"codex": cut}), spot)
            spot.unlink()

    def test_claude_rules_load_from_every_folder_up_the_tree(self):
        start = self.repo / "packages" / "api"
        for rel in ("work/.claude/rules/org.md", "work/repo/.claude/rules/root.md",
                    "work/repo/packages/.claude/rules/mid.md", "work/repo/packages/api/.claude/rules/api.md"):
            write(self.tmp_path / rel, "a rule\n")
        write(start / ".claude" / "rules" / "scoped.md", "---\npaths:\n  - src/**\n---\nonly for src\n")
        write(self.repo / "other" / ".claude" / "rules" / "off.md", "not on the path\n")
        write(self.home / ".claude" / "rules" / "mine.md", "user rule\n")
        write(self.home / ".claude" / "rules" / "scoped.md", "---\npaths:\n  - x/**\n---\nonly for x\n")
        r = self.cw_json("files", str(start), "--harness", "claude")["claude"]
        self.assertEqual(self._claude_rel(r["files"]), [
            ("home/.claude/rules/mine.md", "user rule"),
            ("work/.claude/rules/org.md", "project rule"),
            ("work/repo/.claude/rules/root.md", "project rule"),
            ("work/repo/packages/.claude/rules/mid.md", "project rule"),
            ("work/repo/packages/api/.claude/rules/api.md", "project rule"),
        ])
        self.assertTrue(any("2 path-scoped" in n for n in r["notes"]), r["notes"])
        # In a repo inside the home folder, ~/.claude/rules are the user's rules, counted once.
        repo2 = self.home / "code" / "r2"
        (repo2 / ".git").mkdir(parents=True)
        r = self.cw_json("files", str(repo2), "--harness", "claude")["claude"]
        self.assertEqual(self._claude_rel(r["files"]), [("home/.claude/rules/mine.md", "user rule")])
        self.assertTrue(any("1 path-scoped" in n for n in r["notes"]), r["notes"])

    def test_nested_worktree_skips_the_main_checkout_folders(self):
        # Claude Code's own worktrees live inside the main checkout (.claude/worktrees/<name>) and
        # hold their own copy of its files, so it skips the main checkout's folders there.
        self._git_repo()
        write(self.repo / "CLAUDE.md", "main rules\n")
        write(self.repo / ".claude" / "rules" / "r.md", "a rule\n")
        self._commit("start", "2026-01-01T00:00:00+00:00")
        wt = self.repo / ".claude" / "worktrees" / "feat"
        self._git("worktree", "add", "-q", "-b", "feat", str(wt))
        write(self.repo.parent / "CLAUDE.md", "workspace rules\n")  # above the main checkout: still read
        r = self.cw_json("files", str(wt), "--harness", "claude")["claude"]
        self.assertEqual(self._claude_rel(r["files"]), [
            ("work/CLAUDE.md", "project"),
            ("work/repo/.claude/worktrees/feat/CLAUDE.md", "project"),
            ("work/repo/.claude/worktrees/feat/.claude/rules/r.md", "project rule"),
        ])

    def test_auto_memory_follows_the_repository_and_its_switches(self):
        # One auto memory per git repository, shared by its subfolders and worktrees; it can be
        # turned off or moved.
        self._git_repo()
        write(self.repo / "README.md", "x\n")
        self._commit("start", "2026-01-01T00:00:00+00:00")
        wt = self.repo.parent / "repo-wt"
        self._git("worktree", "add", "-q", "-b", "wt", str(wt))
        (self.repo / "sub").mkdir()
        slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(self.repo.resolve()))
        mem = write(self.home / ".claude" / "projects" / slug / "memory" / "MEMORY.md", "remember this\n").resolve()
        user, project = self.home / ".claude" / "settings.json", self.repo / ".claude" / "settings.json"

        def memory(path, **env):
            saved = dict(self.env)
            self.env.update(env)
            try:
                r = self.cw_json("files", str(path), "--harness", "claude")["claude"]
            finally:
                self.env = saved
            return [Path(f["path"]).resolve() for f in r["files"] if f["role"] == "user memory"]

        for where in (self.repo, self.repo / "sub", wt):
            self.assertEqual(memory(where), [mem], where)
        self.assertEqual(memory(self.repo, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1"), [])
        write(user, json.dumps({"autoMemoryEnabled": False}))
        self.assertEqual(memory(self.repo), [])
        self.assertEqual(memory(self.repo, CLAUDE_CODE_DISABLE_AUTO_MEMORY="0"), [mem])  # set but false: on
        write(user, json.dumps({"autoMemoryEnabled": True}))
        write(project, json.dumps({"autoMemoryEnabled": False}))
        self.assertEqual(memory(self.repo), [])
        project.unlink()
        moved = write(self.tmp_path / "elsewhere" / "MEMORY.md", "moved\n").resolve()
        write(user, json.dumps({"autoMemoryDirectory": str(moved.parent)}))
        self.assertEqual(memory(self.repo), [moved])
        notes = write(self.home / "notes" / "MEMORY.md", "notes\n").resolve()
        write(project, json.dumps({"autoMemoryDirectory": "~/notes"}))  # project settings beat user ones
        self.assertEqual(memory(self.repo), [notes])
        project.unlink()
        user.unlink()
        # CLAUDE_CODE_PROJECT_DIR_NAME beside CLAUDE_CONFIG_DIR names the project folder.
        named = write(self.home / ".claude" / "projects" / "shared" / "memory" / "MEMORY.md", "shared\n").resolve()
        self.assertEqual(memory(self.repo, CLAUDE_CONFIG_DIR=str(self.home / ".claude"),
                                CLAUDE_CODE_PROJECT_DIR_NAME="shared"), [named])
        self.assertEqual(memory(self.repo, CLAUDE_CODE_PROJECT_DIR_NAME="shared"), [mem])  # needs both

    def test_worktree_sessions_count_for_the_repository(self):
        # A session started in a worktree, Claude Code's own (.claude/worktrees/<name>) or a
        # sibling one, is a session in this repository.
        self._git_repo()
        self._slim_history()
        nested = self.repo / ".claude" / "worktrees" / "feat"
        sibling = self.repo.parent / "repo-wt"
        self._git("worktree", "add", "-q", "-b", "feat", str(nested))
        self._git("worktree", "add", "-q", "-b", "wt", str(sibling))
        for i, day in enumerate(("10", "12", "14")):
            self._claude_log(f"b{i}", self.repo, f"2026-01-{day}T12:00:00Z", [80_000])
        for i, (where, day) in enumerate(((nested, "10"), (sibling, "12"), (nested, "14"))):
            self._claude_log(f"a{i}", where, f"2026-02-{day}T12:00:00Z", [30_000])
        for path in (self.repo, nested, sibling):
            res = self.cw_json("report", str(path), "--days", "40000", "--harness", "claude")
            self.assertEqual(res["sessions"]["claude"]["sessions"], 6, path)
            s = res["compare"]["sessions"]["claude"]
            self.assertEqual((s["before"]["sessions"], s["after"]["sessions"]), (3, 3), path)
        groups = self.cw_json("sessions", "--harness", "claude", "--since", "2026-01-01")
        self.assertEqual(sorted(groups), [f"claude {self.repo.resolve()}"])

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
        write(self.home / ".agents" / "skills" / "one" / "SKILL.md", "---\nname: one\ndescription: " + "e" * 1_000 + "\n---\n")
        res = self.cw_json("skills", str(self.repo), "--harness", "claude,codex", "--window", "10000")
        # Claude Code lists hidden folders too, .trash as "- .trash (trash): Skill"; Codex does not
        self.assertEqual(res["claude"]["count"], 2)
        self.assertEqual(res["claude"]["desc_chars"], 1_536 + 5)  # truncated in the listing
        self.assertTrue(res["claude"]["over_budget"])             # budget 300 columns
        self.assertEqual(res["codex"]["count"], 1)
        self.assertTrue(res["codex"]["over_budget"])              # budget 200 tokens

    def test_a_huge_number_in_a_skill_or_config_does_not_crash(self):
        # Python 3.11+ (and patched 3.8 to 3.10) refuse int() past 4,300 digits with ValueError.
        digits = "1" * 5_000
        write(self.home / ".claude" / "skills" / "big" / "SKILL.md", f"---\nname: big\ndescription: {digits}\n---\n")
        write(self.home / ".agents" / "skills" / "big" / "SKILL.md", f"---\nname: big\ndescription: {digits}\n---\n")
        write(self.home / ".codex" / "config.toml", f"other = {digits}\n")
        res = self.cw_json("skills", str(self.repo))
        self.assertEqual((res["claude"]["count"], res["codex"]["count"]), (1, 1))
        cw = load_cw()
        with self.assertRaises(cw._TomlError):  # the reader without tomllib: a value TOML cannot hold
            cw._Toml(f"other = {digits}\n").parse()

    def test_codex_count_includes_skills_left_out_for_room(self):
        for name in ("one", "two"):
            write(self.home / ".agents" / "skills" / name / "SKILL.md",
                  f"---\nname: {name}\ndescription: The {name} helper.\n---\n")
        write(self.home / ".codex" / "config.toml", "[skills]\nmax_context_tokens = 1\n")
        r = self.cw_json("skills", str(self.repo), "--harness", "codex")["codex"]
        self.assertEqual((r["count"], r["omitted"]), (2, 2))
        found = self.cw_json("report", str(self.repo), "--harness", "codex")["findings"]
        self.assertTrue(any(f.startswith("[codex] 2 skills need ") for f in found), found)

    @unittest.skipIf(os.name == "nt", "needs a POSIX named pipe")
    def test_a_named_pipe_is_never_read(self):
        # Opening a named pipe to read waits for a writer: one pipe hung `files` and `report`.
        write(self.home / ".claude" / "commands" / "real.md", "Run it.\n")
        write(self.repo / "CLAUDE.md", "rules\n")
        for pipe in (self.home / ".claude" / "commands" / "pipe.md", self.home / ".claude" / "rules" / "pipe.md",
                     self.repo / ".claude" / "rules" / "pipe.md", self.repo / "AGENTS.md",
                     self.home / ".claude" / "skills" / "p" / "SKILL.md", self.home / ".codex" / "config.toml",
                     self.home / ".claude" / "projects" / "-x" / "pipe.jsonl",
                     self.home / ".codex" / "sessions" / "2099" / "01" / "01" / "rollout-pipe.jsonl"):
            pipe.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(str(pipe))
        for args in (("skills", str(self.repo), "--harness", "claude"), ("files", str(self.repo)),
                     ("report", str(self.repo)), ("sessions",)):
            p = subprocess.run([sys.executable, str(CW), *args, "--json"], env=self.env, capture_output=True,
                               text=True, encoding="utf-8", timeout=60)
            self.assertEqual(p.returncode, 0, (args, p.stderr))
        res = self.cw_json("skills", str(self.repo), "--harness", "claude")
        self.assertEqual(res["claude"]["count"], 1)

    def test_skill_findings_name_the_setting_that_fixes_them(self):
        write(self.home / ".claude" / "skills" / "big" / "SKILL.md",
              "---\nname: big\ndescription: " + "d" * 1_000 + "\n---\n")
        write(self.home / ".agents" / "skills" / "one" / "SKILL.md", "---\nname: one\ndescription: " + "e" * 1_000 + "\n---\n")
        found = self.cw_json("report", str(self.repo), "--harness", "claude,codex", "--window", "10000")["findings"]
        claude = [f for f in found if f.startswith("[claude]") and "listing" in f]
        codex = [f for f in found if f.startswith("[codex]") and "listing" in f]
        self.assertEqual((len(claude), len(codex)), (1, 1), found)
        for want in ("big", "over by", 'skillOverrides {"big": "off"}', "disable-model-invocation: true",
                     "skillListingBudgetFraction"):
            self.assertIn(want, claude[0])
        for want in ("one", "over by", 'a [[skills.config]] entry with name = "one" and enabled = false',
                     "allow_implicit_invocation: false", "max_context_tokens"):
            self.assertIn(want, codex[0])
        out = self.run_cw("skills", str(self.repo), "--harness", "claude,codex", "--window", "10000")
        self.assertIn("fix: ", out)  # the plain output says it too

    def test_project_and_plugin_skills_are_listed(self):
        write(self.repo / ".claude" / "skills" / "local" / "SKILL.md", "---\nname: local\ndescription: hi\n---\n")
        cache = self.home / ".claude" / "plugins" / "cache" / "market" / "tool" / "1.0"
        write(cache / "skills" / "helper" / "SKILL.md", "---\nname: helper\ndescription: hi\n---\n")
        write(self.home / ".claude" / "plugins" / "installed_plugins.json",
              json.dumps({"plugins": {"tool@market": [{"version": "1.0"}]}}))
        for enabled, names in ((True, ["local", "tool:helper"]), (False, ["local"])):
            write(self.home / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"tool@market": enabled}}))
            s = self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]
            self.assertEqual(sorted(h["name"] for h in s["heaviest"]), names, enabled)

    def claude_listing(self, cwd=None, policy=None):
        """Claude Code's listing, from cw.py in this process with this test's HOME and an empty
        managed folder, or `policy`."""
        cw = load_cw()
        managed = Path(policy or self.tmp_path / "no-policy")
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(cw, "_policy_dir", lambda: managed):
            return cw.claude_listing(str(cwd or self.repo))

    def claude_lines(self, cwd=None, policy=None):
        """The lines Claude Code would list, sorted."""
        return sorted(e["line"] for e in self.claude_listing(cwd, policy)["entries"])

    def plugin(self, name, version="1.0"):
        """A plugin folder in Claude Code's cache, recorded as installed (enable it in settings)."""
        root = self.home / ".claude" / "plugins" / "cache" / "market" / name / version
        index = self.home / ".claude" / "plugins" / "installed_plugins.json"
        known = json.loads(index.read_text()) if index.exists() else {"plugins": {}}
        known["plugins"][f"{name}@market"] = [{"version": version}]
        write(index, json.dumps(known))
        return root

    def test_claude_listing_lines_follow_claude_code(self):
        sk = self.home / ".claude" / "skills"
        write(sk / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: Does <b>x</b>\nwhen_to_use: Use it\n---\nbody")
        write(sk / "dir-name" / "SKILL.md", "---\nname: real-name\ndescription: hi\n---\n")
        write(sk / "spaced" / "SKILL.md", "---\nname: Spaced Name\ndescription: hi\n---\n")
        write(sk / "titled" / "SKILL.md", "---\nname: titled\n---\n\n# Big Title\ntext\n")
        write(sk / "wordy" / "SKILL.md", "---\nname: wordy\n---\n" + "v" * 150 + "\n")
        write(sk / "empty" / "SKILL.md", "---\nname: empty\n---\n")
        write(sk / "long" / "SKILL.md", '---\ndescription: "' + "w" * 2_000 + '"\n---\n')
        write(sk / "block" / "SKILL.md", "---\ndescription: |\n  one\n  two\n---\n")
        write(sk / "quoted" / "SKILL.md", "---\ndescription: 'it''s fine' # a comment\n---\n")
        write(sk / "plain" / "SKILL.md", "---\ndescription: plain text # not part\n---\n")
        write(sk / "escaped" / "SKILL.md", '---\ndescription: "tab\\there, \\e[31mred\\e[0m"\n---\n')
        write(sk / "thai" / "SKILL.md", "---\ndescription: \"ภาษา\\u200bไทย\"\n---\n")
        write(sk / "dashes" / "SKILL.md", "---\ndescription: cut---here\nname: never read\n---\n")
        write(sk / "synced" / "x" / "SKILL.md", "---\ndescription: a folder Claude Code keeps for synced skills\n---\n")
        self.assertEqual(self.claude_lines(), sorted([
            "- alpha: Does &lt;b&gt;x&lt;/b&gt; - Use it",        # when_to_use joins with " - "; < and > escaped
            "- dir-name (real-name): hi",                       # listed by folder, the name in brackets
            "- spaced: hi",                                     # a name with a space is not shown
            "- titled: Big Title",                              # no description: the first line, # dropped
            "- wordy: " + "v" * 97 + "...",                     # ... cut at 100 characters
            "- empty: Skill",                                   # or nothing at all
            "- long: " + "w" * 1_535 + "…",                # descriptions stop at 1,536 characters
            "- block: one two",                                 # line breaks become spaces
            "- quoted: it's fine",
            "- plain: plain text",
            "- escaped: tab here, red",                         # tabs become spaces, terminal codes go
            "- thai: ภาษา ไทย",                                 # so does a zero-width space
            "- dashes: cut",                                    # frontmatter ends at the first ---
        ]))

    def test_claude_listing_leaves_out_what_claude_code_hides(self):
        sk = self.home / ".claude" / "skills"
        for name, extra in (("manual", "disable-model-invocation: true"), ("manual2", 'disable-model-invocation: "yes"'),
                            ("auto", "disable-model-invocation: false"), ("off", ""), ("menu", ""), ("terse", ""),
                            ("scoped", "paths: src/**"), ("anywhere", 'paths: "**"'), ("on", "")):
            write(sk / name / "SKILL.md", f"---\ndescription: hi\n{extra}\n---\n")
        tool = self.plugin("tool")
        write(tool / "skills" / "helper" / "SKILL.md", "---\ndescription: hi\n---\n")
        write(tool / "skills" / "bare" / "SKILL.md", "---\nname: bare\n---\nbody line\n")
        write(tool / "skills" / "when" / "SKILL.md", "---\nwhen_to_use: on Mondays\n---\nbody line\n")
        write(self.home / ".claude" / "settings.json", json.dumps({
            "enabledPlugins": {"tool@market": True},
            "skillOverrides": {"off": "off", "menu": "user-invocable-only", "terse": "name-only", "on": "on",
                               "tool:helper": "off"}}))
        self.assertEqual(self.claude_lines(), sorted([
            "- anywhere: hi", "- auto: hi", "- on: hi", "- terse",
            "- tool:helper: hi",                # plugin skills ignore skillOverrides
            "- tool:when: body line - on Mondays"]))  # a plugin skill needs a description or when_to_use
        s = self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual(s["hidden"], {"disable-model-invocation": 2, "skillOverrides": 2, "paths": 1,
                                       "no description": 1})

    def test_claude_listing_reads_every_place_claude_code_does(self):
        sub = self.repo / "pkg" / "app"
        sub.mkdir(parents=True)
        write(self.repo / ".claude" / "skills" / "root-skill" / "SKILL.md", "---\ndescription: r\n---\n")
        write(self.repo / "pkg" / ".claude" / "skills" / "pkg-skill" / "SKILL.md", "---\ndescription: p\n---\n")
        write(self.repo.parent / ".claude" / "skills" / "above" / "SKILL.md", "---\ndescription: a\n---\n")
        cmds = self.home / ".claude" / "commands"
        write(cmds / "review.md", "---\ndescription: Review it\n---\nbody\n")
        write(cmds / "git" / "commit.md", "Commit the staged work\n")
        write(cmds / "blank.md", "")
        write(self.repo / ".claude" / "commands" / "deploy" / "SKILL.md", "---\ndescription: d\n---\n")
        tool = self.plugin("tool")
        write(tool / "commands" / "go.md", "---\ndescription: Go\n---\n")
        write(tool / "commands" / "quiet.md", "no frontmatter\n")
        solo = self.plugin("solo", "2.0")
        write(solo / "SKILL.md", "---\nname: solo\ndescription: s\n---\n")
        extra = self.plugin("extra")
        write(extra / ".claude-plugin" / "plugin.json", json.dumps({"name": "extra", "skills": ["./more"]}))
        write(extra / "more" / "deep" / "SKILL.md", "---\ndescription: e\n---\n")
        write(self.home / ".claude" / "settings.json", json.dumps(
            {"enabledPlugins": {"tool@market": True, "solo@market": True, "extra@market": True}}))
        expected = ["- root-skill: r", "- pkg-skill: p", "- review: Review it", "- git:commit: Commit the staged work",
                    "- blank: Custom command", "- deploy: d", "- tool:go: Go", "- solo:solo: s"]
        self.assertEqual(self.claude_lines(sub), sorted(expected + ["- extra:deep: e"]))
        # enabledPlugins merge per plugin: the project's false beats the user's true
        write(self.repo / ".claude" / "settings.json", json.dumps({"enabledPlugins": {"extra@market": False}}))
        self.assertEqual(self.claude_lines(sub), sorted(expected))

    def test_claude_listing_keeps_plugin_order_and_one_skill_per_name(self):
        zeta, alpha = self.plugin("zeta"), self.plugin("alpha")
        write(zeta / "commands" / "go.md", "---\ndescription: the command\n---\n")
        write(zeta / "skills" / "go" / "SKILL.md", "---\ndescription: the skill\n---\n")
        write(zeta / "skills" / "two" / "SKILL.md", "---\ndescription: z\n---\n")
        write(alpha / "skills" / "one" / "SKILL.md", "---\ndescription: a\n---\n")
        write(self.home / ".claude" / "settings.json",
              json.dumps({"enabledPlugins": {"zeta@market": True, "alpha@market": True}}))
        got = self.claude_listing()
        # Plugins in the order settings name them, every plugin's commands before the skills;
        # a second entry with a name already listed is not listed
        self.assertEqual([e["line"] for e in got["entries"]], ["- zeta:go: the command", "- zeta:two: z", "- alpha:one: a"])
        self.assertEqual(got["hidden"], {"same name": 1})

    def test_claude_listing_reads_the_managed_folder(self):
        policy = self.tmp_path / "policy"
        write(policy / ".claude" / "skills" / "corp" / "SKILL.md", "---\ndescription: c\n---\n")
        write(policy / ".claude" / "commands" / "audit.md", "---\ndescription: a\n---\n")
        self.assertEqual(self.claude_lines(policy=policy), ["- audit: a", "- corp: c"])
        self.env["CLAUDE_CODE_DISABLE_POLICY_SKILLS"] = "0"  # read as a yes or no, like Claude Code
        self.assertEqual(self.claude_lines(policy=policy), ["- audit: a", "- corp: c"])
        self.env["CLAUDE_CODE_DISABLE_POLICY_SKILLS"] = "1"  # managed skills off; managed commands stay
        self.assertEqual(self.claude_lines(policy=policy), ["- audit: a"])

    def test_claude_listing_in_a_worktree_reads_the_main_checkout(self):
        write(self.repo / ".claude" / "skills" / "kept" / "SKILL.md", "---\ndescription: k\n---\n")
        gitdir = self.repo / ".git" / "worktrees" / "wt"
        write(gitdir / "commondir", "../..\n")
        wt = self.tmp_path / "work" / "wt"
        write(wt / ".git", f"gitdir: {gitdir}\n")
        self.assertEqual(self.claude_lines(wt), ["- kept: k"])
        write(wt / ".claude" / "skills" / "own" / "SKILL.md", "---\ndescription: o\n---\n")
        self.assertEqual(self.claude_lines(wt), ["- own: o"])  # it has its own skills folder

    def test_claude_listing_counts_the_account_synced_skills(self):
        base = self.home / ".claude" / "skills" / "synced"
        write(base / "org-1_acct-1" / "pdf" / "SKILL.md", "---\ndescription: p\n---\n")
        write(base / "org-1_unbound" / "docx" / "SKILL.md", "---\ndescription: d\n---\n")
        write(base / "org-2_acct-9" / "xlsx" / "SKILL.md", "---\ndescription: x\n---\n")
        self.assertEqual(self.claude_lines(), [])  # no account: none of them
        write(self.home / ".claude.json", json.dumps({"oauthAccount": {"organizationUuid": "ORG-1", "accountUuid": "ACCT-1"}}))
        self.assertEqual(self.claude_lines(), ["- anthropic-skills:pdf: p"])
        write(self.home / ".claude.json", json.dumps({"oauthAccount": {"organizationUuid": "org-1"}}))
        self.assertEqual(self.claude_lines(), ["- anthropic-skills:docx: d"])

    def test_claude_budget_follows_the_model(self):
        write(self.home / ".claude" / "skills" / "one" / "SKILL.md", "---\ndescription: hi\n---\n")

        def budget(*args):
            s = self.cw_json("skills", str(self.repo), "--harness", "claude", *args)["claude"]
            return s["budget"], s["window"], s["chars_per_token"], s["model"], s["window_source"]

        # Nothing says which model: today's default, a 1M window at 3 characters per token.
        self.assertEqual(budget(), (30_000, 1_000_000, 3, None, "assumed"))
        settings = self.home / ".claude" / "settings.json"
        write(settings, json.dumps({"model": "claude-haiku-4-5-20251001"}))
        self.assertEqual(budget(), (8_000, 200_000, 4, "claude-haiku-4-5-20251001", "model"))
        write(settings, json.dumps({"model": "claude-sonnet-4-6[1m]"}))
        self.assertEqual(budget(), (40_000, 1_000_000, 4, "claude-sonnet-4-6[1m]", "model"))
        self.env["ANTHROPIC_MODEL"] = "claude-new-9"   # beats settings; unknown: 200k at 3
        self.assertEqual(budget(), (6_000, 200_000, 3, "claude-new-9", "model"))
        del self.env["ANTHROPIC_MODEL"]
        write(settings, json.dumps({"model": "opus"}))  # an alias names no model
        self.assertEqual(budget(), (30_000, 1_000_000, 3, None, "assumed"))
        write(self.home / ".claude" / "projects" / "-elsewhere" / "a.jsonl", json.dumps(
            {"type": "assistant", "cwd": str(self.tmp_path), "requestedModel": "claude-opus-4-1",
             "message": {"id": "a", "model": "claude-opus-4-1"}}) + "\n")
        self.assertEqual(budget()[:4], (8_000, 200_000, 4, "claude-opus-4-1"))  # another project's last session
        log = self.home / ".claude" / "projects" / "-repo" / "b.jsonl"
        write(log, json.dumps({"type": "assistant", "cwd": str(self.repo), "requestedModel": "claude-opus-5-5",
                               "message": {"id": "b", "model": "claude-opus-5-5"}}) + "\n")
        os.utime(log, (1, 1))  # older, but from this project
        self.assertEqual(budget(), (30_000, 1_000_000, 3, "claude-opus-5-5", "model"))
        self.assertEqual(budget("--window", "400000")[:2], (12_000, 400_000))
        write(settings, json.dumps({"env": {"CLAUDE_CODE_DISABLE_1M_CONTEXT": "1"}}))
        self.assertEqual(budget()[:2], (6_000, 200_000))
        write(settings, json.dumps({"env": {"DISABLE_COMPACT": "1", "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "50000"}}))
        self.assertEqual(budget()[:2], (1_500, 50_000))
        write(settings, json.dumps({"env": {"SLASH_COMMAND_TOOL_CHAR_BUDGET": "12k"}}))  # read as Claude Code does: 12
        self.assertEqual(budget()[0], 12)

    def test_claude_listing_over_budget_says_how_many_lose_descriptions(self):
        sk = self.home / ".claude" / "skills"
        for name, size in (("aa", 100), ("bb", 200), ("cc", 300)):
            write(sk / name / "SKILL.md", f"---\ndescription: {'d' * size}\n---\n")
        # Lines of 106, 206 and 306 columns and two line breaks: 620. A line that loses its
        # description is "- aa", 4 columns.
        for budget, losing in (("1000", []), ("400", ["cc"]), ("300", ["bb", "cc"]), ("10", ["aa", "bb", "cc"])):
            self.env["SLASH_COMMAND_TOOL_CHAR_BUDGET"] = budget
            s = self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]
            self.assertEqual((s["used"], s["over_budget"], s["lose_descriptions"], s["losing_descriptions"]),
                             (620, bool(losing), len(losing), losing), budget)
        # Descriptions go back to the most used first, by Claude Code's record: cc ran today
        now = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
        write(self.home / ".claude.json", json.dumps({"skillUsage": {"cc": {"usageCount": 3, "lastUsedAt": now}}}))
        self.env["SLASH_COMMAND_TOOL_CHAR_BUDGET"] = "400"
        s = self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual(s["losing_descriptions"], ["aa", "bb"])

    def test_listing_width_counts_columns_as_claude_code(self):
        cw = load_cw()
        for text, cols in (("abc", 3), ("กิน", 2), ("漢字", 4), ("👍🏽", 2), ("🇹🇭", 2), ("é", 1),
                           ("❤️", 2), ("1️⃣", 2), ("👨‍👩‍👧", 2), ("​", 0), ("a\tb", 2)):
            self.assertEqual(cw.str_width(text), cols, ascii(text))

    def test_frontmatter_yaml_reads_as_bun_does(self):
        # Each expected value is what Bun.YAML.parse (Bun 1.4.4, inside Claude Code 2.1.322)
        # returned for the same text; None stands for an error.
        cw = load_cw()
        for text, want in (
                ("a: 1\na: 2", {"a": 2}),                      # a repeated key keeps the last value
                ("a: {b: 1, b: 2}", {"a": {"b": 2}}),
                ("a: |+\n  one\n\n\n", {"a": "one\n\n\n"}),    # the last break ends a line, not adds one
                ("a: 'one\n  two'", {"a": "one two"}),
                ("a: 'one\ntwo'", None),                       # a quoted value's next line must be indented
                ("a: [b\n  c]", {"a": ["b c"]}),
                ("a: [b,\nc]", None),                          # so must a flow list's
                ("a: [\n  b\n]", {"a": ["b"]}),                # except its closing bracket
                ("a: [b, &x c, *x]", {"a": ["b", "c", "c"]}),
                ("a: ,b", {"a": ",b"}),
                ("a: !!str", {"a": ""}),
                ("a: b: c", None),
                ("...\na: b", [None, {"a": "b"}]),             # ... ends a document
                ("...\n...\na: b", [None, {"a": "b"}]),
                ('a: "x\\uD83D\\uDE00y"', {"a": "x\U0001F600y"}),
                ('a: "x\\uD83Dy"', None),                      # half of a surrogate pair
        ):
            try:
                got = cw._cc_yaml(text)
            except cw._YamlError:
                got = None
            self.assertEqual(got, want, ascii(text))

    # ------------------------------------------------------------------ Codex skill listing
    def test_codex_frontmatter_reads_as_serde_yaml_does(self):
        # Codex 0.166.0 reads SKILL.md frontmatter with serde_yaml 0.9.34 into
        # {name, description, metadata: {short-description}}; None stands for a file it refuses.
        cw = load_cw()
        fm = "---\nname: a\ndescription: {}\n---\n"
        for text, want in (
                (fm.format("b"), ("a", "b")),
                ("---\nname: 123\ndescription: true\n---\n", ("123", "true")),  # text, not numbers
                ("---\nname: ~\ndescription: hi\n---\n", ("dir", "hi")),        # no name: the folder's
                ("---\nname: '~'\ndescription: hi\n---\n", ("~", "hi")),
                ("---\nname: a\nname: b\ndescription: hi\n---\n", None),         # a field given twice
                ("---\nx: 1\nx: 2\nname: a\ndescription: hi\n---\n", ("a", "hi")),  # others may repeat
                ("---\nname: a\ndescription: hi\nmetadata:\n---\n", ("a", "hi")),
                ("---\nname: a\ndescription: hi\nmetadata: ~\n---\n", None),     # metadata must be a mapping
                ("---\nname: a\ndescription: hi\nmetadata: [x]\n---\n", None),
                ("---\nname: a\ndescription: hi\nmetadata:\n  short-description: [x]\n---\n", None),
                ("---\nname: [a]\ndescription: hi\n---\n", None),
                (fm.format("Build for AWS: ECS # note"), ("a", "Build for AWS: ECS")),  # repaired
                (fm.format("@here"), ("a", "@here")),
                (fm.format('"x"y'), None),
                ("---\nname: a\ndescription: |\n  one: two\n  three\n---\n", ("a", "one: two three")),
                (fm.format('"one\\u001ftwo\\u3000three"'), ("a", "one\x1ftwo three")),  # Rust's whitespace
                ("---\nname: a\n---\n", None),                                   # a description is required
                (fm.format("'  '"), None),
                ("---\n# only a comment\n---\n", None),
                ("---\n---\n", None),
                ("---\nname: a\ndescription: hi\n", None),                       # never closed
                ("﻿" + fm.format("hi"), None),                              # a BOM hides the frontmatter
                ("  ---  \r\nname: a\r\ndescription: hi\r\n---\r\n", ("a", "hi")),
                ("---\nname: " + "n" * 65 + "\ndescription: hi\n---\n", None),   # a name over 64 characters
                ("---\nname: a\ndescription: x\n...\nname: b\n---\n", None),     # two documents
                ("---\n- a\n- b\n---\n", None),
        ):
            self.assertEqual(cw._codex_frontmatter(text, "dir"), want, ascii(text))

    def codex_listing(self, cwd=None, admin=None):
        """Codex's listing, from cw.py in this process with this test's HOME and an empty
        /etc/codex, or `admin`."""
        cw = load_cw()
        managed = Path(admin or self.tmp_path / "no-admin")
        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(cw, "_codex_admin_dir", lambda: managed):
            return cw.codex_listing(str(cwd or self.repo))

    def codex_shown(self, text: str) -> str:
        h = os.path.realpath(str(self.home)).replace(os.sep, "/")
        r = os.path.realpath(str(self.repo)).replace(os.sep, "/")
        return text.replace(r, "<repo>").replace(h, "~")

    def codex_raw(self, cwd=None, admin=None):
        """(the roots table, the lines) as Codex would write them, with this test's home as ~ and
        the repo as <repo>."""
        r = self.codex_listing(cwd, admin)
        return [self.codex_shown(x) for x in r["roots"]], [self.codex_shown(e["line"]) for e in r["entries"]]

    def codex_lines(self, cwd=None, admin=None):
        """The lines Codex would list, in its order, each root alias written out in full."""
        roots, lines = self.codex_raw(cwd, admin)
        for root in roots:
            alias, path = re.match(r"- `(r[0-9]+)` = `(.*)`\Z", root).groups()
            lines = [x.replace(f"(file: {alias}/", f"(file: {path}/") for x in lines]
        return lines

    def symlink(self, link: Path, target: Path) -> bool:
        """A folder symlink, or False where this system cannot make one (Windows without rights)."""
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            return False

    def test_codex_listing_lines_follow_codex(self):
        sk = self.home / ".agents" / "skills"
        write(sk / "alpha" / "SKILL.md", "---\nname: alpha\ndescription: |\n  Does   x\n    and y\n---\nbody")
        write(sk / "dir-name" / "SKILL.md", "---\ndescription: hi\n---\n")
        write(sk / "colon" / "SKILL.md", "---\nname: colon\ndescription: Build for AWS: ECS # note\n---\n")
        write(sk / "long" / "SKILL.md", "---\nname: long\ndescription: " + "w" * 2_000 + "\n---\n")
        write(sk / "typed" / "SKILL.md", "---\nname: 123\ndescription: true\n---\n")
        write(sk / "a" / "b" / "c" / "d" / "e" / "deep" / "SKILL.md", "---\nname: deep\ndescription: six folders down\n---\n")
        write(sk / "a" / "b" / "c" / "d" / "e" / "f" / "deeper" / "SKILL.md", "---\nname: deeper\ndescription: seven\n---\n")
        write(sk / ".hidden" / "x" / "SKILL.md", "---\nname: hidden\ndescription: no\n---\n")
        write(sk / "lower" / "skill.md", "---\nname: lower\ndescription: no\n---\n")
        write(sk / "nodesc" / "SKILL.md", "---\nname: nodesc\n---\n")
        write(sk / "open" / "SKILL.md", "---\nname: open\ndescription: never closed\n")
        write(sk / "bom" / "SKILL.md", "﻿---\nname: bom\ndescription: hi\n---\n")
        write(sk / "twice" / "SKILL.md", "---\nname: twice\nname: again\ndescription: hi\n---\n")
        # Paths are written from a root alias: Codex's roots table costs less than the full paths
        self.assertEqual(self.codex_raw(), (["- `r0` = `~/.agents/skills`"], [
            "- 123: true (file: r0/typed/SKILL.md)",
            "- alpha: Does x and y (file: r0/alpha/SKILL.md)",
            "- colon: Build for AWS: ECS (file: r0/colon/SKILL.md)",
            "- deep: six folders down (file: r0/a/b/c/d/e/deep/SKILL.md)",
            "- dir-name: hi (file: r0/dir-name/SKILL.md)",
            "- long: " + "w" * 1_021 + "... (file: r0/long/SKILL.md)",
        ]))
        self.assertEqual(self.codex_listing()["hidden"], {"unreadable": 4})

    def test_codex_roots_scopes_and_order(self):
        codex = self.home / ".codex"
        skill = "---\nname: {}\ndescription: hi\n---\n"
        write(codex / "skills" / ".system" / "sys" / "SKILL.md", skill.format("zsys"))
        admin = self.tmp_path / "admin"
        write(admin / "skills" / "adm" / "SKILL.md", skill.format("yadm"))
        write(self.repo / ".agents" / "skills" / "r1" / "SKILL.md", skill.format("xrepo"))
        write(self.repo / "sub" / ".codex" / "skills" / "r2" / "SKILL.md", skill.format("wrepo"))
        write(self.home / ".agents" / "skills" / "u1" / "SKILL.md", skill.format("a-user"))
        write(codex / "skills" / "u2" / "SKILL.md", skill.format("b-user"))
        sub = self.repo / "sub"
        names = lambda **kw: [ln.split(":")[0][2:] for ln in self.codex_lines(**kw)]
        # System, Admin, Repo, then User; by name within each
        self.assertEqual(names(cwd=sub, admin=admin), ["zsys", "yadm", "wrepo", "xrepo", "a-user", "b-user"])
        self.assertEqual(names(cwd=self.repo), ["zsys", "xrepo", "a-user", "b-user"])  # .codex/skills below cwd
        write(codex / "config.toml", "project_root_markers = []\n\n[skills.bundled]\nenabled = false\n")
        self.assertEqual(names(cwd=sub), ["wrepo", "a-user", "b-user"])  # the project root is now cwd
        # One skill under two roots is listed once, from the root Codex reads first: the user's
        # folders come before the repo's .agents/skills
        if self.symlink(self.home / ".agents" / "skills" / "link", self.repo / ".agents" / "skills" / "r1"):
            lines = self.codex_lines()
            self.assertEqual([ln for ln in lines if "xrepo" in ln], ["- xrepo: hi (file: ~/.agents/skills/link/SKILL.md)"])

    def test_codex_config_rules_and_openai_yaml(self):
        sk = self.home / ".agents" / "skills"
        for name in ("gone", "back", "quiet", "chat", "both", "broken", "plain"):
            write(sk / name / "SKILL.md", f"---\nname: {name}\ndescription: hi\n---\n")
        write(sk / "quiet" / "agents" / "openai.yaml", "policy:\n  allow_implicit_invocation: false\n")
        write(sk / "chat" / "agents" / "openai.yaml", "policy:\n  products: [chatgpt]\n")
        write(sk / "both" / "agents" / "openai.yaml", "policy:\n  products: [chatgpt, CODEX]\n")
        write(sk / "broken" / "agents" / "openai.yaml", "policy: [\n")  # unreadable: the defaults apply
        back = json.dumps(str(sk / "back" / "SKILL.md"))
        write(self.home / ".codex" / "config.toml",
              '[[skills.config]]\nname = "gone"\nenabled = false\n\n'
              f"[[skills.config]]\npath = {back}\nenabled = false\n\n"
              f"[[skills.config]]\npath = {back}\nenabled = true\n")  # the last rule for a path wins
        r = self.codex_listing()
        self.assertEqual([e["name"] for e in r["entries"]], ["back", "both", "broken", "plain"])
        self.assertEqual(r["hidden"], {"skills.config": 1, "allow_implicit_invocation": 1, "products": 1})
        write(self.home / ".codex" / "config.toml", "[skills]\ninclude_instructions = false\n")
        r = self.codex_listing()
        self.assertEqual((r["entries"], r["hidden"]), ([], {"allow_implicit_invocation": 1, "products": 1,
                                                  "skills.include_instructions": 5}))

    def test_codex_plugins_and_namespaces(self):
        cache = self.home / ".codex" / "plugins" / "cache" / "market"
        skill = "---\nname: {}\ndescription: hi\n---\n"
        for version in ("1.0", "2.0", "local"):  # "local" wins over any version
            write(cache / "tool" / version / ".codex-plugin" / "plugin.json", json.dumps({"name": "tool"}))
            write(cache / "tool" / version / "skills" / version / "SKILL.md", skill.format("a" + version))
        write(cache / "off" / "1.0" / ".codex-plugin" / "plugin.json", json.dumps({"name": "off"}))
        write(cache / "off" / "1.0" / "skills" / "o" / "SKILL.md", skill.format("o"))
        for version in ("1.2.0", "1.10.0"):  # the highest version, compared as versions
            root = cache / "more" / version
            write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "more", "skills": "./extra"}))
            write(root / "extra" / "b" / "SKILL.md", skill.format("b"))
            write(root / "skills" / "c" / "SKILL.md", skill.format("c"))  # not read: the manifest names ./extra
        write(self.home / ".codex" / "config.toml",
              '[plugins."tool@market"]\nenabled = true\n\n[plugins."off@market"]\nenabled = false\n\n'
              '[plugins."more@market"]\nenabled = true\n')
        write(self.repo / ".claude-plugin" / "plugin.json", json.dumps({"name": "mine"}))  # the repo is a plugin
        write(self.repo / ".agents" / "skills" / "x" / "SKILL.md", skill.format("x"))
        want = ["- mine:x: hi (file: <repo>/.agents/skills/x/SKILL.md)",
                "- more:b: hi (file: ~/.codex/plugins/cache/market/more/1.10.0/extra/b/SKILL.md)",
                "- tool:alocal: hi (file: ~/.codex/plugins/cache/market/tool/local/skills/local/SKILL.md)"]
        self.assertEqual(self.codex_lines(), want)
        # Plugin versions with one listed skill share their marketplace folder's alias
        self.assertEqual(self.codex_raw()[0], ["- `r0` = `~/.codex/plugins/cache/market`",
                                               "- `r1` = `<repo>/.agents/skills`"])
        # A skill folder linked from a plugin's checkout takes that plugin's name
        dev = self.tmp_path / "dev" / "plug"
        write(dev / ".codex-plugin" / "plugin.json", json.dumps({"name": "dev"}))
        write(dev / "skills" / "linked" / "SKILL.md", skill.format("linked"))
        if self.symlink(self.home / ".agents" / "skills" / "linked", dev / "skills" / "linked"):
            self.assertEqual(self.codex_lines(),
                             [want[0], "- dev:linked: hi (file: ~/.agents/skills/linked/SKILL.md)", *want[1:]])

    def test_codex_budget_cuts_descriptions_then_omits(self):
        sk = self.home / ".agents" / "skills"
        write(sk / "a" / "SKILL.md", "---\nname: a\ndescription: " + "x" * 400 + "\n---\n")
        write(sk / "b" / "SKILL.md", "---\nname: b\ndescription: " + "y" * 40 + "\n---\n")
        config = self.home / ".codex" / "config.toml"
        cost = lambda line: -(-(len(line.encode()) + 1) // 4)  # Codex: a line and its break, 4 bytes a token
        full = self.codex_listing()
        self.assertEqual((full["budget"], full["budget_unit"], full["window_source"], full["aliases"]),
                         (5_440, "tokens", "assumed", 1))
        self.assertEqual(full["used"], sum(cost(e["line"]) for e in full["entries"]))
        minimum = sum(cost(e["line"].split(": ", 1)[0] + ": " + e["line"][e["line"].index("(file: "):])
                      for e in full["entries"])
        write(config, f"[skills]\nmax_context_tokens = {minimum + 30}\n")
        cut = self.codex_listing()
        lines = {e["name"]: e["line"] for e in cut["entries"]}
        self.assertIn("- b: " + "y" * 40 + " (file: ", lines["b"])           # the short one keeps all of it
        self.assertNotIn("x" * 400, lines["a"])                              # the long one is cut
        self.assertIn("- a: xxxx", lines["a"])
        self.assertLessEqual(cut["loaded"], cut["budget"])  # what Codex sends fits
        self.assertGreater(cut["used"], cut["budget"])  # what the skills ask for does not
        self.assertEqual((cut["budget"], cut["over_budget"], cut["losing_descriptions"], cut["omitted"]),
                         (minimum + 30, True, ["a"], 0))
        write(config, "[skills]\nmax_context_tokens = 1\n")
        none = self.codex_listing()
        self.assertEqual((none["entries"], none["omitted"]), ([], 2))
        write(config, "[skills]\nmax_context_tokens = 20000\n")
        self.assertEqual(self.codex_listing()["budget"], 10_000)              # Codex caps it at 10,000

    # ------------------------------------------------------------------ sessions
    def _claude_log(self, name, cwd, ts, ctxs, dup_first=0, creation=0):
        recs = [{"type": "user", "cwd": str(cwd), "timestamp": ts}]
        for i, c in enumerate(ctxs):
            rec = {"type": "assistant", "cwd": str(cwd), "timestamp": ts, "isSidechain": False,
                   "message": {"id": f"{name}-m{i}", "usage": {
                       "input_tokens": 1, "cache_read_input_tokens": c - 1 - creation,
                       "cache_creation_input_tokens": creation}}}
            recs += [rec] * (1 + (dup_first if i == 0 else 0))
        recs.append({"type": "assistant", "isSidechain": True, "timestamp": ts,
                     "message": {"id": "side", "usage": {"input_tokens": 999_999}}})
        jsonl(self.home / ".claude" / "projects" / "-repo" / f"{name}.jsonl", recs)

    def _codex_log(self, name, cwd, ts, ctxs, subagent=False, archived=False):
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
        folder = self.home / ".codex" / ("archived_sessions" if archived else "sessions/2026/11/01")
        jsonl(folder / f"rollout-{name}.jsonl", recs)

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

    def test_session_numbers(self):
        # Context per request is input + cache read + cache creation. Eleven requests, so the
        # 90th percentile is neither the median nor the largest.
        ctxs = [10_000 * i for i in range(1, 11)] + [200_000]
        self._claude_log("s", self.repo, "2026-10-01T00:00:00Z", ctxs, creation=500)
        s = self.cw_json("sessions", "--harness", "claude", "--threshold", "150000")[f"claude {self.repo.resolve()}"]
        total = 750_000
        self.assertEqual(s, {"sessions": 1, "continued_sessions": 0, "requests": 11, "baseline_median": 10_000,
                             "ctx_median": 60_000, "ctx_p90": 100_000, "ctx_max": 200_000,
                             "over_threshold_requests": 1 / 11, "over_threshold_tokens": 200_000 / total,
                             "input_tokens": total, "startup_share": min(1.0, 10_000 * 11 / total)})

    def test_sessions_table_and_project_filter(self):
        self._claude_log("s", self.repo, "2026-10-01T00:00:00Z", [10_000, 200_000])
        self._codex_log("old", self.repo, "2026-10-02T00:00:00Z", [20_000, 30_000], archived=True)
        out = self.run_cw("sessions", "--threshold", "150000")
        # The ten number columns are the last ten: the project path may hold spaces.
        rows = {ln.split()[0]: ln.split()[-10:] for ln in out.splitlines() if ln.startswith(("claude ", "codex "))}
        self.assertEqual(rows["claude"], ["1", "2", "10.0k", "105.0k", "200.0k", "200.0k", "50%", "95%", "10%", "210.0k"])
        self.assertEqual(rows["codex"][:2], ["1", "2"])  # an archived rollout still counts
        hits = self.run_cw("sessions", "--project", "work" + os.sep + "repo")
        self.assertEqual(sorted(ln.split()[0] for ln in hits.splitlines() if ln.startswith(("claude ", "codex "))),
                         ["claude", "codex"])
        self.assertEqual(self.run_cw("sessions", "--project", "no-such-project").strip(), "no sessions found")

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
                           cwd=str(self.repo), env=env, capture_output=True, text=True, encoding="utf-8")
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
        # No CLAUDE.md yet, so Claude Code read AGENTS.md, unless a CLAUDE.md sits above.
        self.assertEqual(c["files"]["claude"]["before"], 0 if self._claude_md_above() else 2)
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
            self._claude_log(f"a{i}", self.repo, f"2026-02-{day}T12:00:00Z", [30_000])
        # The report names the local day: 11 February east of UTC+12, so work it out as the child will.
        first = dt.datetime(2026, 2, 10, 12, tzinfo=dt.timezone.utc).astimezone().date().isoformat()
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual(c["logs_from"], {"claude": first})
        out = self.run_cw("report", str(self.repo), "--days", "40000", "--harness", "claude")
        self.assertIn("cleanupPeriodDays", out)
        self.assertIn("30 days", out)
        self.assertEqual((c["cleanup_days"], c["logs_deleted"]), (30, True))
        write(self.home / ".claude" / "settings.json", '{"cleanupPeriodDays": 100000}')
        c = self.cw_json("report", str(self.repo), "--days", "40000", "--harness", "claude")["compare"]
        self.assertEqual((c["logs_from"], c["cleanup_days"], c["logs_deleted"]), ({"claude": first}, 100000, False))
        out = self.run_cw("report", str(self.repo), "--days", "40000", "--harness", "claude")
        self.assertNotIn("deletes transcripts", out)
        self.assertIn(f"logs on this machine start {first}", out)
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
        cw = load_cw()
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
                           input=signed, capture_output=True, text=True, encoding="utf-8", env=self.env)
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
                           cwd=str(here), env=env, capture_output=True, text=True, encoding="utf-8")
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

    def test_report_leads_with_a_verdict_and_at_most_three_actions(self):
        write(self.repo / "CLAUDE.md", "a" * 60_000)
        write(self.repo / "AGENTS.md", "b" * 40_000)  # Codex cuts it
        write(self.home / ".claude" / "skills" / "big" / "SKILL.md", "---\nname: big\ndescription: " + "d" * 1_000 + "\n---\n")
        write(self.repo / "GEMINI.md", "c" * 30_000)
        self._claude_log("s", self.repo, "2099-01-01T00:00:00Z", [90_000, 95_000])
        out = self.tmp_path / "pic.svg"
        res = self.cw_json("report", str(self.repo), "--days", "40000", "--window", "10000", "--svg", str(out))
        v = res["verdict"]
        self.assertEqual(v["level"], "crit")
        self.assertTrue(v["headline"].startswith("Fix this first: "), v)
        self.assertGreater(len(res["findings"]), 3)
        root, texts = self._svg_texts(out)
        ns = "{http://www.w3.org/2000/svg}"
        order = ["".join(t.itertext()) for t in root.iter(ns + "text")]
        at = lambda s: next(i for i, t in enumerate(order) if s in t)
        self.assertLess(at(v["headline"]), at("Where the startup tokens come from"))
        self.assertLess(at("Where the startup tokens come from"), at("What to do"))
        self.assertLess(at("What to do"), at("Details"))
        self.assertIn("1.", order)
        self.assertIn("3.", order)
        self.assertNotIn("4.", order)
        self.assertIn(f"{len(res['findings']) - 3} more in the text report", texts)
        tips = [t.text for t in root.iter(ns + "title")]
        self.assertTrue(any(t and "; skill listing ~" in t for t in tips), tips)
        self.assertIn("skill listing ~", texts)  # also written out, not only in a tooltip
        text = self.run_cw("report", str(self.repo), "--days", "40000", "--window", "10000")
        self.assertEqual(text.splitlines()[2], "Verdict: " + v["headline"])

    def test_report_verdict_when_all_is_well(self):
        write(self.repo / "AGENTS.md", "Run the tests.\n")
        res = self.cw_json("report", str(self.repo), "--harness", "codex")
        self.assertEqual((res["verdict"]["level"], res["findings"]), ("good", []))
        self.assertIn("within target", res["verdict"]["headline"])

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

    def test_report_says_when_startup_is_over_target_with_nothing_large_to_fix(self):
        # ~6.9k tokens: past the 5.0k target, but under the 2,000 slack before a finding is raised.
        write(self.repo / "AGENTS.md", "Run the tests before every commit.\n" * 590, exact=True)
        out = self.tmp_path / "pic.svg"
        res = self.cw_json("report", str(self.repo), "--harness", "codex", "--svg", str(out))
        self.assertEqual(res["findings"], [])
        _, texts = self._svg_texts(out)
        self.assertIn("Codex is ~1.9k over the ~5.0k target", texts)
        self.assertNotIn("within target", texts)
        self.assertIn("Codex is ~1.9k over the ~5.0k target", self.run_cw("report", str(self.repo), "--harness", "codex"))
        write(self.repo / "AGENTS.md", "Run the tests before every commit.\n" * 100, exact=True)
        self.run_cw("report", str(self.repo), "--harness", "codex", "--svg", str(out))
        self.assertIn("Nothing to fix: startup context is within target.", self._svg_texts(out)[1])

    def test_report_svg_bars_match_the_numbers_and_hide_the_home_folder(self):
        write(self.home / ".claude" / "CLAUDE.md", "g" * 9_000)   # 3,000 tokens outside the repo
        write(self.repo / "CLAUDE.md", "a" * 60_000)              # 20,000 tokens in it
        out = self.tmp_path / "pic.svg"
        res = self.cw_json("report", str(self.repo), "--harness", "claude", "--svg", str(out))
        f = res["files"]["claude"]
        repo_t, other_t = f["repo_tokens"], f["total_tokens"] - f["repo_tokens"]
        theirs = f["total_tokens"] - sum(e["tokens"] for e in self.mine(f["files"]))  # above the temp folder
        self.assertEqual((repo_t, other_t - theirs), (20_000, 3_000))
        root, texts = self._svg_texts(out)
        ns = "{http://www.w3.org/2000/svg}"
        widths = {}
        for p in root.iter(ns + "path"):
            tip = p.find(ns + "title")
            if tip is None or ("; this repo ~" not in tip.text and "; outside ~" not in tip.text):
                continue  # the heaviest-file bars use the same classes
            h, r = re.match(r"M[\d.]+,[\d.]+h([\d.]+)a([\d.]+),", p.get("d")).groups()
            widths[p.get("class")] = float(h) + float(r)
        # The outside bar starts after a 2px gap and is drawn that much narrower.
        self.assertAlmostEqual(widths["repo"] / (widths["other"] + 2), repo_t / other_t, delta=0.02)
        tilde = os.path.join("~", ".claude", "CLAUDE.md")  # ~\.claude\CLAUDE.md on Windows
        self.assertIn(tilde, texts)
        for shown in (out.read_text(encoding="utf-8"), self.run_cw("report", str(self.repo), "--harness", "claude")):
            self.assertIn(tilde, shown)
            self.assertNotIn(str(self.home), shown)

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
        cw = load_cw()
        bad = "/x/caf\udce9 a\x01b\ufffe"
        rep = {"project": bad, "since": "2026-01-01", "days": 30, "target": 5000, "sessions": {},
               "files": {"claude": {"files": [{"path": bad + "/CLAUDE.md", "role": "project", "tokens": 9000}],
                                    "total_tokens": 9000, "repo_tokens": 9000, "warnings": [], "notes": []}},
               "skills": {"claude": {"budget": 8000, "budget_unit": "chars", "chars": 8320, "used": 8320,
                                     "tokens": 2000, "window_source": "--window"}},
               "findings": ["[claude] odd \x03 character"]}
        out = cw.write_picture(rep, str(self.tmp_path / "odd.svg"), "auto")
        _, texts = self._svg_texts(out)
        self.assertIn("4% over budget", texts)
        self.assertIn("odd \ufffd character", texts)

    def test_report_svg_unwritable_exits_2(self):
        bad = self.tmp_path / "no" / "such" / "dir" / "a.svg"
        p = subprocess.run([sys.executable, str(CW), "report", str(self.repo), "--svg", str(bad)],
                           env=self.env, capture_output=True, text=True, encoding="utf-8")
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
        self.assertEqual(sorted(Path(f["path"]).name for f in self.mine(r["files"])),
                         ["CLAUDE.md", "My Notes.md", "notes(v2).md"])

    def _memory(self, text):
        slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in str(self.repo.resolve()))
        write(self.home / ".claude" / "projects" / slug / "memory" / "MEMORY.md", text)
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        mem = [f for f in r["files"] if f["role"] == "user memory"]
        self.assertEqual(len(mem), 1)
        return mem[0]

    def test_auto_memory_counted_first_200_lines(self):
        self.assertEqual(self._memory("abc\n" * 500)["lines"], 200)

    def test_auto_memory_counted_first_25_kb(self):
        mem = self._memory(("m" * 199 + "\n") * 150)  # 150 lines of 200 bytes: the 25 KB cap comes first
        self.assertEqual((mem["bytes"], mem["lines"]), (25_000, 125))

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

    def test_verify_split_counts_a_line_that_opens_with_inline_code(self):
        # CommonMark: a backtick fence's info string has no backtick, so this line is prose, not a fence.
        span = "```make deploy``` ships to production, never on Fridays"
        old = write(self.repo / "old.md", f"Rules:\n{span}\nTag the release first\n")
        new = write(self.repo / "AGENTS.md", "Rules:\nTag the release first\n")
        res = self.cw_json("verify-split", str(old), str(new), code=1)
        self.assertEqual((res["missing"], res["checked"]), ([span], 3))

    def test_verify_split_fence_closes_only_on_its_own_marker(self):
        # Inside a ``` block a ~~~ line is code, and so is a shorter run of the same character.
        for fence, inner in (("```", "~~~"), ("````", "```")):
            old = write(self.repo / "old.md", f"{fence}yaml\n{inner}\n  prod: blue\n{fence}\n")
            new = write(self.repo / "AGENTS.md", f"{fence}yaml\n{inner}\nprod: blue\n{fence}\n")
            res = self.cw_json("verify-split", str(old), str(new), code=1)
            self.assertEqual(res["missing"], ["  prod: blue"], fence)
        # cmark-gfm counts at most 255 of a fence's characters, so 256 close one of 300 and the
        # next 300 open a block.
        long_fence = "`" * 300
        old_text = f"{long_fence}\nx\n{'`' * 256}\n{long_fence}\nenv:\n  prod: blue\n{long_fence}\n"
        old = write(self.repo / "old.md", old_text)
        new = write(self.repo / "AGENTS.md", old_text.replace("  prod", "prod"))
        self.assertEqual(self.cw_json("verify-split", str(old), str(new), code=1)["missing"], ["  prod: blue"])

    def test_verify_split_fence_indented_four_more_is_code(self):
        # CommonMark: a closing fence has at most three spaces of indentation. Four or more is a code line.
        old = write(self.repo / "old.md", "```yaml\nsteps:\n    ```\n  prod: blue\n```\n")
        new = write(self.repo / "AGENTS.md", "```yaml\nsteps:\n    ```\nprod: blue\n```\n")
        self.assertEqual(self.cw_json("verify-split", str(old), str(new), code=1)["missing"], ["  prod: blue"])
        # In a list item the three spaces count from where the item's text starts, column 2 for
        # `- deploy:`: a block opened at column 4 closes at column 5, not at 7. After a fence at 7
        # the block goes on, so the indented line is code and must keep its indentation.
        for close, missing in (("       ```", ["  after the block"]), ("     ```", [])):
            old = write(self.repo / "old.md", f"- deploy:\n\n    ```sh\n    make  ship\n{close}\n      after the block\n")
            new = write(self.repo / "AGENTS.md", f"- deploy:\n\n    ```sh\n    make ship\n{close}\nafter the block\n")
            res = self.cw_json("verify-split", str(old), str(new), code=1 if missing else 0)
            self.assertEqual(res["missing"], missing, close)

    def test_verify_split_finds_fences_where_list_items_put_them(self):
        cases = {
            # An item can open with a fence; its code starts where the item's text does, column 3.
            "item opens with a fence": ("1. ```sh\n   make\n     ship\n   ```\n",
                                        "1. ```sh\n   make\n   ship\n   ```\n", ["  ship"]),
            # A block left open ends with its item, so the next fence opens a block, not closes one.
            "block ends with its item": ("- build:\n  ```sh\n  make\n- config:\n```yaml\nenv:\n  prod: blue\n```\n",
                                         "- build:\n  ```sh\n  make\n- config:\n```yaml\nenv:\nprod: blue\n```\n",
                                         ["  prod: blue"]),
            # Four spaces in at the margin is no fence, so the bare fence after it opens the block.
            "four spaces in is no fence": ("Text\n    ```\n```\nenv:\n  prod: blue\n```\n",
                                           "Text\n    ```\n```\nenv:\nprod: blue\n```\n", ["  prod: blue"]),
        }
        for name, (old_text, new_text, missing) in cases.items():
            old = write(self.repo / "old.md", old_text)
            new = write(self.repo / "AGENTS.md", new_text)
            self.assertEqual(self.cw_json("verify-split", str(old), str(new), code=1)["missing"], missing, name)

    def _split_cases(self, cases):
        old, new = self.repo / "old.md", self.repo / "AGENTS.md"
        for name, (old_text, new_text, missing) in cases.items():
            with self.subTest(name):
                old.write_bytes(old_text.encode("utf-8"))  # these exact line endings, on Windows too
                new.write_bytes(new_text.encode("utf-8"))
                res = self.cw_json("verify-split", str(old), str(new), code=1 if missing else 0)
                self.assertEqual(res["missing"], missing)

    def test_verify_split_lazy_lines_keep_their_list_item(self):
        # CommonMark: a paragraph line indented less than its item's text ("lazy") stays in the
        # item, so a fence after it still sits in the item and its code keeps its indentation.
        yaml, flat = "    ```yaml\n    env:\n      prod: blue\n    ```\n", "    ```yaml\n    env:\n    prod: blue\n    ```\n"
        py = "    ```python\n    def ship():\n        return deploy()\n    ```\n"
        cases = {
            "10.": ("10. Configure the service; this sentence wraps\nonto the next line.\n\n" + py,
                    "10. Configure the service; this sentence wraps\nonto the next line.\n\n"
                    + py.replace("        return", "    return"), ["    return deploy()"]),
            "two spaces after 1.": ("1.  Deploy with the steps below; it wraps\nonto a second line.\n\n" + yaml,
                                    "1.  Deploy with the steps below; it wraps\nonto a second line.\n\n" + flat,
                                    ["  prod: blue"]),
            "nested": ("- Deploy:\n  - Run the steps below; it wraps\nonto a lazy line.\n\n" + yaml,
                       "- Deploy:\n  - Run the steps below; it wraps\nonto a lazy line.\n\n" + flat, ["  prod: blue"]),
            "under-indented, no blank": ("1.  Configure the service; it wraps\n  onto a short line.\n" + yaml,
                                         "1.  Configure the service; it wraps\n  onto a short line.\n" + flat,
                                         ["  prod: blue"]),
        }
        self._split_cases(cases)

    def test_verify_split_compares_indented_code_as_code(self):
        # Four spaces past where the text of its container starts, after a blank line, is an
        # indented code block (a fence there is a line of that code), so indentation counts.
        cases = {
            "margin": ("Run this:\n\n    ```yaml\n    env:\n      prod: blue\n    ```\n",
                       "Run this:\n\n    ```yaml\n    env:\n    prod: blue\n    ```\n", ["  prod: blue"]),
            "in an item": ("- Deploy:\n\n      ```yaml\n      env:\n        prod: blue\n      ```\n",
                           "- Deploy:\n\n      ```yaml\n      env:\n      prod: blue\n      ```\n", ["  prod: blue"]),
            "plain code": ("Ship it:\n\n    def ship():\n        return deploy()\n\nDone.\n",
                           "Ship it:\n\n    def ship():\n    return deploy()\n\nDone.\n", ["    return deploy()"]),
            # The same line right under a paragraph continues it: prose, compared without spacing.
            "under a paragraph": ("Ship it:\n    def ship():\n        return deploy()\n",
                                  "Ship it:\n    def ship():\n    return deploy()\n", []),
            # An item opened empty ends at a blank line even after a line of spaces as deep as
            # its text, so the code below sits at the margin, two spaces in.
            "after an empty item": ("-\n   \n\n      x: 1\n", "-\n   \n\n    x: 1\n", ["  x: 1"]),
            # A list number is ASCII digits: a full-width one is text, so the code below is code.
            "a full-width number": ("１. Steps\n\n    def f():\n        return 1\n",
                                    "１. Steps\n\n    def f():\n    return 1\n", ["    return 1"]),
        }
        self._split_cases(cases)

    def test_verify_split_keeps_tabs_in_code(self):
        # A tab in code stays a tab (a Makefile recipe needs one); a tab that only indents an
        # indented code block, or that a container's prefix splits, counts as its columns.
        make = "```make\nbuild:\n\tgo build\n```\n"
        cases = {
            "tab to spaces": (make, make.replace("\t", "    "), ["\tgo build"]),
            "tab dropped": (make, make.replace("\t", ""), ["\tgo build"]),
            "into a list item": (make, "- Build:\n\n" + "".join("  " + ln + "\n" for ln in make.splitlines()), []),
            "indented code": ("Run:\n\n\tmake build\n", "Run:\n\n    make build\n", []),
            # The item takes two columns of the first tab and the code four more: two are left.
            "the rest of a split tab": ("- Run:\n\n\t\tmake build\n", "- Run:\n\n```\n  make build\n```\n", []),
            # cmark-gfm strips one column per character of a fence's indentation. The item's text
            # starts two columns into the tab, which leaves two: one is stripped, so GitHub shows
            # " build:" and " \tgo build".
            "a fence indented with a tab": (make, "- Build:\n" + "".join("\t" + ln + "\n" for ln in make.splitlines()),
                                            ["build:", "\tgo build"]),
            # Under `1. ` the tab has one column left, and it is stripped: no change.
            "under 1.": (make, "1. Build:\n" + "".join("\t" + ln + "\n" for ln in make.splitlines()), []),
            # Under `100. ` the second tab has three left: two show, "  build:".
            "under 100.": (make, "100. Build:\n" + "".join("\t\t" + ln + "\n" for ln in make.splitlines()),
                           ["build:", "\tgo build"]),
        }
        self._split_cases(cases)

    def test_verify_split_shows_tabs_and_control_characters(self):
        # The text output must show a tab that became spaces, and must not pass a file's escape
        # sequences to the terminal. --json keeps the lines as they are.
        old = write(self.repo / "old.md", "```make\nbuild:\n\tgo build\n```\nRun \x1b[31mnow\x1b[0m‮\n")
        new = write(self.repo / "AGENTS.md", "```make\nbuild:\n    go build\n```\n")
        out = self.run_cw("verify-split", str(old), str(new), code=1)
        self.assertIn("\n  - \\tgo build\n", out)
        self.assertIn("\n  - Run \\x1b[31mnow\\x1b[0m\\u202e\n", out)
        self.assertNotIn("\x1b", out)
        res = self.cw_json("verify-split", str(old), str(new), code=1)
        self.assertEqual(res["missing"], ["\tgo build", "Run \x1b[31mnow\x1b[0m‮"])

    def test_verify_split_a_dash_under_text_is_a_heading_rule(self):
        # `Some text` over `-` is a setext heading, not an empty list item, so the fence after it
        # sits at the margin and the next bare fence opens a block instead of closing one.
        self._split_cases({"setext": ("Some text\n-\n  ```\ncode at margin\n  ```\n\n```\nenv:\n  prod: blue\n```\n",
                                      "Some text\n-\n  ```\ncode at margin\n  ```\n\n```\nenv:\nprod: blue\n```\n",
                                      ["  prod: blue"])})

    def test_verify_split_finds_fences_in_block_quotes(self):
        quoted = "> [!IMPORTANT]\n> ```yaml\n> env:\n>   prod: blue\n> ```\n"
        cases = {
            "alert": (quoted, quoted.replace(">   prod", "> prod"), ["  prod: blue"]),
            "moved out of the quote": (quoted, "> [!IMPORTANT]\n```yaml\nenv:\n  prod: blue\n```\n", []),
            "in a quoted item": ("> - Deploy:\n>   ```yaml\n>   env:\n>     prod: blue\n>   ```\n",
                                 "> - Deploy:\n>   ```yaml\n>   env:\n>   prod: blue\n>   ```\n", ["  prod: blue"]),
        }
        self._split_cases(cases)

    def test_verify_split_a_table_is_not_a_paragraph(self):
        # GFM: a table ends the paragraph its header row was in, so a line four spaces past the
        # text under a row is indented code, and a list numbered 2 can start there.
        table = "| Env | Color |\n| --- | :---: |\n| prod | blue |\n"
        code, flat = "    env:\n      prod: blue\n", "    env:\n    prod: blue\n"
        cases = {
            "code under a table": (table + code, table + flat, ["  prod: blue"]),
            "in a list item": ("- Colors:\n" + table.replace("|", "  |", 1).replace("\n|", "\n  |") + "  " + code.replace("\n ", "\n   "),
                               "- Colors:\n" + table.replace("|", "  |", 1).replace("\n|", "\n  |") + "  " + flat.replace("\n ", "\n   "),
                               ["  prod: blue"]),
            "an item numbered 2": (table + "2. Then:\n\n       env:\n         prod: blue\n",
                                   table + "2. Then:\n\n       env:\n       prod: blue\n", ["  prod: blue"]),
            # No table: the delimiter row has one cell, the header two (`\|` is no separator),
            # so the lines stay one paragraph and its indented lines are prose.
            "cells differ": ("| Env | Color |\n| --- |\n" + code, "| Env | Color |\n| --- |\n" + flat, []),
            "escaped pipe": ("Env \\| Color\n--- | ---\n" + code, "Env \\| Color\n--- | ---\n" + flat, []),
        }
        self._split_cases(cases)

    def test_verify_split_lines_end_only_at_line_endings(self):
        # A form feed or a Unicode line separator inside a line does not end it (str.splitlines
        # would split there and open a fence that is not one); \r\n and a lone \r do.
        yaml = "```\nenv:\n  prod: blue\n```\n"
        cases = {
            "line separator": ("Note: see below ```\n" + yaml, "Note: see below ```\n" + yaml.replace("  prod", "prod"),
                               ["  prod: blue"]),
            "form feed": ("Note: see below\x0c```\n" + yaml, "Note: see below\x0c```\n" + yaml.replace("  prod", "prod"),
                          ["  prod: blue"]),
            "CRLF": (yaml.replace("\n", "\r\n"), yaml.replace("  prod", "prod"), ["  prod: blue"]),
            "CR": (yaml.replace("\n", "\r"), yaml.replace("  prod", "prod"), ["  prod: blue"]),
        }
        self._split_cases(cases)

    def test_verify_split_html_blocks(self):
        # CommonMark HTML blocks: a fence inside one opens nothing, and a line after one that has
        # ended is not paragraph text. Types 1 to 5 run past blank lines to their end marker;
        # types 6 (a block tag such as div) and 7 (any other tag alone on its line) end before a
        # blank line, and type 7 cannot interrupt a paragraph.
        code, flat = "```\nenv:\n  prod: blue\n```\n", "```\nenv:\nprod: blue\n```\n"
        cases = {
            "comment, then indented code": ("<!-- generated: do not edit -->\n    def f():\n        return 1\n",
                                            "<!-- generated: do not edit -->\n    def f():\n    return 1\n",
                                            ["    return 1"]),
        }
        for start, end in (("<pre>", "</pre>"), ("<!-- TODO", "-->"), ("<?php", "?>"), ("<!DOCTYPE", ">"),
                           ("<![CDATA[", "]]>")):
            html = f"{start}\n\n```\n{end}\n\n"
            cases[start] = (html + code, html + flat, ["  prod: blue"])
        for start in ("Text\n<div>", "<example>"):  # a div interrupts a paragraph
            html = f"{start}\n```\n\n"
            cases[start] = (html + code, html + flat, ["  prod: blue"])
        item = "- Deploy:\n  <!-- example\n  ```\n  -->\n\n  ```\n  env:\n    prod: blue\n  ```\n"
        cases["in a list item"] = (item, item.replace("    prod", "  prod"), ["  prod: blue"])
        details = "<details>\n<summary>Deploy</summary>\n\n"
        cases["a div ends at a blank line"] = (details + code + "\n</details>\n", details + flat + "\n</details>\n",
                                               ["  prod: blue"])
        cases["a tag alone does not interrupt a paragraph"] = ("Text\n<example>\n" + code, "Text\n<example>\n" + flat,
                                                               ["  prod: blue"])
        link = '<a href="https://example.com">Docs</a> for the API\n'
        cases["a tag with text after it"] = (link + code, link + flat, ["  prod: blue"])
        # Tag names are ASCII: Python's re.I would let a long s, a dotless i or the Kelvin sign
        # stand for s, i and k, and cmark-gfm reads these lines as text.
        for tag in ("<ſcript>\n\n", "<dıv>\n", "<blocKquote>\n"):
            cases[tag.strip()] = (tag + code, tag + flat, ["  prod: blue"])
        # So is an end tag: `</ſcript>` leaves the script block open until `</script>`.
        script = "<script>\n</ſcript>\n```\n</script>\n\n"
        cases["an end tag in look-alike letters"] = (script + code, script + flat, ["  prod: blue"])
        # A CDATA start is read in any letter case, as cmark-gfm reads it.
        cases["a lower-case CDATA start"] = ("<![cdata[\n```\n]]>\n\n" + code, "<![cdata[\n```\n]]>\n\n" + flat,
                                             ["  prod: blue"])
        cases["a quote that ends ends its comment"] = ("> <!-- note\n" + code, "> <!-- note\n" + flat, ["  prod: blue"])
        self._split_cases(cases)

    def test_verify_split_allow_list_ignores_spacing(self):
        old = write(self.repo / "old.md", "keep me\ndropped   on purpose\n")
        new = write(self.repo / "AGENTS.md", "keep me\n")
        allow = write(self.repo / "dropped.txt", "  dropped on   purpose  \n")
        self.run_cw("verify-split", str(old), str(new), "--allow", str(allow))

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
                           env=self.env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("never sees", p.stderr)

    # ------------------------------------------------------------------ robustness
    def test_bom_does_not_hide_frontmatter(self):
        write(self.repo / ".claude" / "rules" / "scoped.md", "﻿---\npaths:\n  - src/**\n---\nonly src")
        write(self.home / ".claude" / "skills" / "dir-name" / "SKILL.md", "﻿---\nname: real-name\ndescription: hi\n---\n")
        r = self.cw_json("files", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual(self.mine(r["files"]), [])
        s = self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual([h["name"] for h in s["heaviest"]], ["dir-name (real-name)"])

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
               '{"type": "assistant", "message": {"id": "inf", "usage": {"input_tokens": 1e999}}}',
               '{"type": "assistant", "cut off']
        write(self.home / ".claude" / "projects" / "-repo" / "x.jsonl", "\n".join(bad + [json.dumps(good)]) + "\n")
        meta = {"type": "session_meta", "payload": {"cwd": str(self.repo)}}
        tok = {"type": "event_msg", "timestamp": "2026-10-01T00:00:00Z", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"total_tokens": 5}, "last_token_usage": {"input_tokens": 2_000}}}}
        cbad = ['["token_count"]', '{"type": "session_meta", "payload": []}',
                json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": []}}),
                json.dumps({"type": "event_msg", "payload": {"type": "token_count",
                                                              "info": {"last_token_usage": {"input_tokens": None}}}}),
                '{"type": "event_msg", "payload": {"type": "token_count", "info": {'
                '"last_token_usage": {"input_tokens": 1e999}, "model_context_window": 1e999}}}']
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
        self.assertEqual([Path(f["path"]).name for f in self.mine(r["files"])], ["style.md"])

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
        # 2% of the full window: Codex logs the 95% it lets a session use, so 400,000 is 421,053
        self.assertEqual(s["codex"]["budget"], 8_421)
        self.assertEqual(s["claude"]["window_source"], "assumed")  # no model in Claude logs or settings
        s = self.cw_json("skills", str(self.repo), "--harness", "claude,codex", "--window", "1000000")
        # Claude Code budgets the listing in columns: 1% of the window at 3 characters per token
        self.assertEqual((s["claude"]["budget"], s["claude"]["budget_unit"]), (30_000, "chars"))
        # Codex records its own window; --window must not override it (it once did: 20,000)
        self.assertEqual((s["codex"]["budget"], s["codex"]["budget_unit"]), (8_421, "tokens"))
        self.assertEqual(s["codex"]["window_source"], "codex logs")
        write(self.home / ".claude" / "settings.json", json.dumps({"skillListingBudgetFraction": 0.02}))
        self.assertEqual(self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]["budget"], 60_000)
        self.env["SLASH_COMMAND_TOOL_CHAR_BUDGET"] = "123456"
        s = self.cw_json("skills", str(self.repo), "--harness", "claude")["claude"]
        self.assertEqual((s["budget"], s["basis"]), (123_456, "SLASH_COMMAND_TOOL_CHAR_BUDGET"))

    # ------------------------------------------------------------------ installer
    def run_install(self, *args, code=0):
        p = subprocess.run([sys.executable, str(CW.parents[3] / "install.py"), *args],
                           env=self.env, capture_output=True, text=True, encoding="utf-8")
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
                           capture_output=True, text=True, encoding="utf-8")
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
                               env=self.env, capture_output=True, text=True, encoding="utf-8")
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
        self.run_cw("report", str(self.repo), "--target", "-5000", code=2)
        # An empty path is an unset variable in a CI line, not the current folder.
        for cmd in ("files", "skills", "report"):
            self.run_cw(cmd, "", code=2)
        old = write(self.repo / "old.md", "a rule found nowhere else\n")
        self.run_cw("verify-split", str(old), "", code=2)

    def test_a_harness_named_twice_counts_once(self):
        self._claude_log("s", self.repo, "2026-10-01T00:00:00Z", [10_000, 20_000])
        once = self.cw_json("sessions", "--harness", "claude")
        self.assertEqual(self.cw_json("sessions", "--harness", "claude,claude"), once)
        res = self.cw_json("report", str(self.repo), "--harness", "claude, claude", "--days", "40000")
        self.assertEqual(res["sessions"]["claude"]["requests"], 2)

    def test_huge_days_window_reaches_back_to_year_one(self):
        self._git_repo()
        self._slim_history()
        self._claude_log("s", self.repo, "2026-02-10T12:00:00Z", [30_000])
        for days in ("740000", "99999999999999999999"):
            res = self.cw_json("report", str(self.repo), "--days", days)
            self.assertEqual(res["since"], "0001-01-01", days)
            self.assertEqual(res["compare"]["sessions"]["claude"]["after"]["sessions"], 1, days)
            self.assertIn("sessions since 0001-01-01", self.run_cw("report", str(self.repo), "--days", days))

    @unittest.skipIf(os.name == "nt", "closing a pipe early is a POSIX shell habit (`| head`)")
    def test_a_closed_pipe_keeps_the_exit_code(self):
        old = write(self.repo / "old.md", "".join(f"rule {i:05d}: keep the tests green\n" for i in range(3000)))
        write(self.repo / "AGENTS.md", "nothing here\n")
        cases = ((["verify-split", str(old), str(self.repo / "AGENTS.md"), "--json"], 1),
                 (["verify-split", str(old), str(self.repo / "AGENTS.md")], 1),
                 (["files", str(self.repo), "--max-tokens", "1"], 1),
                 (["files", str(self.repo)], 0),
                 (["files", str(self.repo / "missing")], 2),
                 (["files", str(self.repo), "--max-tokens", "lots"], 2),  # argparse's own error
                 (["--help"], 0),
                 (["--version"], 0))
        for args, code in cases:
            # `| head -0`, and `2>&1 | head -0`, where errors go into the same closed pipe
            for together in (False, True):
                p = subprocess.Popen([sys.executable, str(CW), *args], env=self.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT if together else subprocess.PIPE)
                p.stdout.close()  # the reader is gone before cw.py writes a byte
                err = "" if together else p.stderr.read().decode("utf-8", errors="replace")
                if not together:
                    p.stderr.close()
                self.assertEqual(p.wait(), code, (args, together, err))
                self.assertNotIn("Exception ignored", err, args)
                self.assertNotIn("Traceback", err, args)

    @unittest.skipIf(os.name == "nt", "no fd to close before exec on Windows")
    def test_a_closed_stdout_keeps_the_exit_code(self):
        # `cw.py ... >&-` (or pythonw) starts Python with no stdout at all: sys.stdout is None.
        write(self.repo / "AGENTS.md", "a" * 3_000)  # ~1,000 tokens
        cases = ((["files", str(self.repo), "--max-tokens", "2000"], 0),
                 (["files", str(self.repo), "--max-tokens", "500"], 1),
                 (["files", str(self.repo), "--max-tokens", "2000", "--json"], 0),
                 (["files", str(self.repo / "missing")], 2))
        for args, code in cases:
            p = subprocess.run([sys.executable, str(CW), *args], env=self.env, stderr=subprocess.PIPE,
                               preexec_fn=lambda: os.close(1))
            err = p.stderr.decode("utf-8", errors="replace")
            self.assertEqual(p.returncode, code, (args, err))
            self.assertNotIn("Traceback", err, args)

    @unittest.skipIf(os.name == "nt", "no RLIMIT_FSIZE on Windows")
    def test_a_failed_write_exits_2(self):
        # A full disk or a file size limit is not a reader that left: the output is lost, so the
        # run fails whatever the check decided. `ulimit -f 0` makes every write to a file fail.
        import resource
        old = write(self.repo / "old.md", "".join(f"rule {i:05d}: keep the tests green\n" for i in range(3000)))
        write(self.repo / "AGENTS.md", "nothing here\n")
        cases = (["verify-split", str(old), str(self.repo / "AGENTS.md"), "--json"],  # fails while printing
                 ["files", str(self.repo)],  # fails on the last flush
                 ["files", str(self.repo), "--max-tokens", "1"],
                 ["--help"], ["--version"])
        limit = lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))  # noqa: E731
        # -B: Apple's Python 3.9 writes bytecode under ~/Library/Caches as it starts, before Python
        # ignores SIGXFSZ, and the fake home has none yet, so the limit would kill it first.
        command = [sys.executable, "-B", str(CW)]
        for args in cases:
            with open(self.tmp_path / "out.txt", "wb") as out:
                p = subprocess.run(command + args, env=self.env, stdout=out, stderr=subprocess.PIPE, preexec_fn=limit)
                both = subprocess.run(command + args, env=self.env, stdout=out,
                                      stderr=subprocess.STDOUT, preexec_fn=limit)  # `> out 2>&1`: no error can show
            err = p.stderr.decode("utf-8", errors="replace")
            self.assertEqual(p.returncode, 2, (args, err))
            self.assertIn("cw.py: error: cannot write output", err, args)
            self.assertNotIn("Traceback", err, args)
            self.assertNotIn("Exception ignored", err, args)
            self.assertEqual(both.returncode, 2, args)

    def test_git_variables_from_a_hook_do_not_redirect_compare(self):
        self._git_repo()
        self._slim_history()
        self._claude_log("s", self.repo, "2026-02-10T12:00:00Z", [30_000])
        expected = self.cw_json("report", str(self.repo), "--days", "40000")["compare"]
        decoy = self.tmp_path / "decoy"
        decoy.mkdir()
        self._git("-C", str(decoy), "init", "-q")
        # A pre-commit hook runs with these set to the repository being committed.
        self.env.update(GIT_DIR=str(decoy / ".git"), GIT_WORK_TREE=str(decoy),
                        GIT_INDEX_FILE=str(decoy / ".git" / "index"))
        self.assertEqual(self.cw_json("report", str(self.repo), "--days", "40000")["compare"], expected)

    def test_project_typed_in_other_letter_case_is_one_project(self):
        other = self.repo.parent / "REPO"
        if not os.path.isdir(str(other)):
            self.skipTest("this disk tells letter cases apart")
        self._git_repo()
        self._slim_history()
        for i, day in enumerate(("10", "12", "14")):
            self._claude_log(f"a{i}", other, f"2026-02-{day}T12:00:00Z", [30_000])
        self._codex_log("x", self.repo, "2026-02-20T12:00:00Z", [20_000])
        for typed in (self.repo, other):
            res = self.cw_json("report", str(typed), "--days", "40000")
            self.assertEqual(sorted(res["sessions"]), ["claude", "codex"], typed)
            self.assertEqual(res["sessions"]["claude"]["sessions"], 3, typed)
            self.assertEqual(res["compare"]["sessions"]["claude"]["after"]["sessions"], 3, typed)

    def test_subfolder_typed_in_other_letter_case_keeps_its_before_and_after(self):
        # git's paths carry the true case; a report on `SUB` must still find the cut of sub/CLAUDE.md.
        if not os.path.isdir(str(self.repo.parent / "REPO")):
            self.skipTest("this disk tells letter cases apart")
        self._git_repo()
        write(self.repo / "sub" / "CLAUDE.md", rules("rule", 600), exact=True)
        self._commit("rules", "2026-01-01T00:00:00+00:00")
        write(self.repo / "sub" / "CLAUDE.md", "short\n", exact=True)
        slim = self._commit("slim", "2026-02-01T00:00:00+00:00")
        cut = (slim, [{"path": "sub/CLAUDE.md", "before": 20_400, "after": 2}])
        for typed in ("sub", "SUB"):
            c = self.cw_json("report", str(self.repo / typed), "--days", "40000", "--harness", "claude")["compare"]
            self.assertEqual((c["commit"], c["changed"]), cut, typed)
        # A parent folder that can be entered but not listed keeps its typed spelling; the folders
        # below it are still spelled as the disk spells them.
        if os.name != "nt" and os.geteuid() != 0:
            os.chmod(str(self.repo.parent), 0o311)
            try:
                c = self.cw_json("report", str(self.repo / "SUB"), "--days", "40000", "--harness", "claude")["compare"]
            finally:
                os.chmod(str(self.repo.parent), 0o755)
            self.assertEqual((c["commit"], c["changed"]), cut, "parent not listable")
        # So does a parent typed in another Unicode form than the disk keeps (U+00E9 for e and
        # U+0301): macOS takes both for one folder.
        disk, typed = self.tmp_path / "cafe\u0301", self.tmp_path / "caf\u00e9"
        disk.mkdir()
        if os.path.isdir(str(typed)) and "caf\u00e9" not in os.listdir(str(self.tmp_path)):
            self._git("clone", "-q", str(self.repo), str(disk / "repo"))
            c = self.cw_json("report", str(typed / "repo" / "SUB"), "--days", "40000", "--harness", "claude")["compare"]
            self.assertEqual((c["commit"], c["changed"]), cut, "parent in another Unicode form")

    def test_default_window_is_the_last_30_days(self):
        # Every other test passes --days 40000; this one runs on dates relative to today.
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

        def ago(days):
            return now - dt.timedelta(days=days)

        self._git_repo()
        write(self.repo / "CLAUDE.md", rules("rule", 600))
        self._commit("rules", ago(20).isoformat())
        write(self.repo / "CLAUDE.md", rules("rule", 60))
        self._commit("slim", ago(10).isoformat())
        elsewhere = self.tmp_path / "work" / "other"
        (elsewhere / ".git").mkdir(parents=True)
        for d in (14, 13, 12, 45):
            self._claude_log(f"b{d}", self.repo, ago(d).strftime("%Y-%m-%dT%H:%M:%SZ"), [80_000])
        for d in (8, 7, 6):
            self._claude_log(f"a{d}", self.repo, ago(d).strftime("%Y-%m-%dT%H:%M:%SZ"), [30_000])
            self._claude_log(f"o{d}", elsewhere, ago(d).strftime("%Y-%m-%dT%H:%M:%SZ"), [99_000])
        res = self.cw_json("report", str(self.repo), "--harness", "claude")
        self.assertEqual(res["sessions"]["claude"]["sessions"], 6)  # not the 45-day-old one, not the other repo
        b, a = res["compare"]["sessions"]["claude"]["before"], res["compare"]["sessions"]["claude"]["after"]
        self.assertEqual((b["sessions"], a["sessions"]), (3, 3))
        self.assertEqual((b["baseline_median"], a["baseline_median"]), (80_000, 30_000))
        self.assertNotIn("compare", self.cw_json("report", str(self.repo), "--harness", "claude", "--days", "5"))

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
        out = subprocess.run([sys.executable, str(CW), "--version"], capture_output=True, text=True, encoding="utf-8")
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

    def test_claude_plugin_icon_exists(self):
        # Anthropic's directory reads the icon from plugin.json, once, when the plugin is first saved.
        icon = self.load(self.FILES[0]).get("icon")
        self.assertTrue(icon)
        self.assertTrue((self.ROOT / icon).is_file(), icon)

    def test_skill_stays_small(self):
        # SKILL.md loads whole each time the skill triggers: it must not regrow unnoticed.
        text = (self.ROOT / "skills" / "context-weight" / "SKILL.md").read_text(encoding="utf-8")
        cw = load_cw()
        desc = cw.frontmatter(text).get("description", "")
        self.assertTrue(desc)
        self.assertLessEqual(len(desc), 1_024)  # Agent Skills spec limit; Claude Code's listing cuts at 1,536
        self.assertLessEqual(cw.est_tokens(text), 3_000)


if __name__ == "__main__":
    unittest.main()
