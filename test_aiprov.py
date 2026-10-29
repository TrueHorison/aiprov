"""Tests for aiprov. Run: python -m unittest -v test_aiprov.py

Each test builds a throwaway git repo with synthetic commits.
If AIPROV_SCHEMA_DIR points at a folder with bom-1.6.schema.json and
spdx-3.0.1-schema.json and the jsonschema package is installed, the outputs
are also checked against the official schemas (optional).
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import aiprov  # noqa: E402

SECRET = "zebra-quartz-77"  # placed in commit subjects and bodies; must never appear in output


class Repo:
    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="aiprov-test-")
        self.env = dict(os.environ)
        self.env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_TERMINAL_PROMPT": "0"})
        self.git("init", "-q", "-b", "main")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "user.name", "Human Dev")
        self.git("config", "user.email", "human@example.com")
        self.n = 0

    def git(self, *args, env=None):
        p = subprocess.run(["git", "-C", self.path, *args], capture_output=True,
                           env=env or self.env)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode())
        return p.stdout.decode().strip()

    def write(self, rel, text):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def commit(self, files, message, author=("Human Dev", "human@example.com"),
               committer=None):
        for rel, text in files.items():
            self.write(rel, text)
        self.git("add", "-A")
        env = dict(self.env)
        env["GIT_AUTHOR_NAME"], env["GIT_AUTHOR_EMAIL"] = author
        cn, ce = committer or author
        env["GIT_COMMITTER_NAME"], env["GIT_COMMITTER_EMAIL"] = cn, ce
        self.n += 1
        msgfile = os.path.join(self.path, ".git", f"msg{self.n}")
        with open(msgfile, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(message)
        self.git("commit", "-q", "-F", msgfile, env=env)
        return self.git("rev-parse", "HEAD")

    def note(self, sha, text):
        nf = os.path.join(self.path, ".git", "note.txt")
        with open(nf, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        self.git("notes", "--ref=ai", "add", "-f", "-F", nf, sha)

    def close(self):
        shutil.rmtree(self.path, ignore_errors=True)


def lines(n, tag="l"):
    return "".join(f"{tag}{i}\n" for i in range(n))


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = aiprov.main(list(args))
    return code, out.getvalue(), err.getvalue()


def uses_of(commit):
    return {u.name: u for u in commit.uses}


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Repo()
        self.base = self.r.commit({"README": "base\n"}, "base")
        self.r.git("tag", "v1")

    def tearDown(self):
        self.r.close()

    def walk(self):
        return aiprov.walk(self.r.path, "v1", "HEAD")


class TestDetection(Base):
    def test_claude_code_trailers(self):
        self.r.commit({"a.py": lines(3)},
                      f"feat {SECRET}\n\nbody {SECRET}\n\n"
                      "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
                      "Claude-Session: https://claude.ai/code/session_01ABC\n")
        (c,) = self.walk()
        u = uses_of(c)["Claude Code"]
        self.assertEqual(u.model, "Claude Opus 5.5")
        self.assertEqual(u.vendor, "Anthropic")
        self.assertEqual(u.model_provider, "Anthropic")
        self.assertEqual(u.evidence, "trailer")
        self.assertEqual(len(c.uses), 1)
        self.assertIn("Claude-Session: https://claude.ai/code/session_01ABC", u.traces)

    def test_claude_session_only_and_unversioned(self):
        self.r.commit({"a.py": "x\n"}, "s\n\nClaude-Session: https://claude.ai/code/session_X\n")
        self.r.commit({"b.py": "x\n"}, "s\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>\n")
        c1, c2 = self.walk()
        self.assertIsNone(uses_of(c1)["Claude Code"].model)
        self.assertIsNone(uses_of(c2)["Claude Code"].model)
        self.assertEqual(uses_of(c2)["Claude Code"].providers(), ["Anthropic"])

    def test_copilot_agent(self):
        self.r.commit({"a.py": lines(2)},
                      "fix\n\nAgent-Logs-Url: https://github.com/o/r/sessions/abc\n\n"
                      "Co-authored-by: someone <1+someone@users.noreply.github.com>\n",
                      author=("Copilot", "198982749+Copilot@users.noreply.github.com"))
        (c,) = self.walk()
        self.assertEqual(list(uses_of(c)), ["GitHub Copilot coding agent"])
        u = c.uses[0]
        self.assertEqual(u.vendor, "GitHub")
        self.assertTrue(any("Agent-Logs-Url" in t for t in u.traces))
        self.assertFalse(any("someone" in t for t in u.traces))  # human co-author not kept

    def test_copilot_squash_merge(self):
        self.r.commit({"a.py": lines(4)},
                      "Big change (#1)\n\n* step one\n\n"
                      "Agent-Logs-Url: https://github.com/o/r/sessions/s1\n\n"
                      "Co-authored-by: dev <2+dev@users.noreply.github.com>\n\n"
                      "---------\n\n"
                      "Co-authored-by: copilot-swe-agent[bot] <198982749+Copilot@users.noreply.github.com>\n")
        (c,) = self.walk()
        self.assertEqual(list(uses_of(c)), ["GitHub Copilot coding agent"])

    def test_claude_agent_on_github(self):
        self.r.commit({"a.py": "x\n"}, "fix\n\nAgent-Logs-Url: https://github.com/o/r/sessions/s2\n",
                      author=("Claude", "242468646+Claude@users.noreply.github.com"))
        (c,) = self.walk()
        self.assertEqual(list(uses_of(c)), ["Claude agent on GitHub"])

    def test_unnamed_github_agent(self):
        self.r.commit({"a.py": "x\n"}, "fix\n\nAgent-Logs-Url: https://github.com/o/r/sessions/s3\n")
        (c,) = self.walk()
        self.assertEqual(list(uses_of(c)), ["GitHub cloud agent (unnamed)"])

    def test_cursor(self):
        self.r.commit({"a.py": "x\n"}, "a\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n")
        self.r.commit({"b.py": "x\n"}, "b\n\nMade-with: Cursor\n")
        c1, c2 = self.walk()
        self.assertEqual(uses_of(c1)["Cursor"].vendor, "Anysphere")
        self.assertIn("Cursor", uses_of(c2))

    def test_codex(self):
        self.r.commit({"a.py": "x\n"}, "a\n\nCo-authored-by: Codex <noreply@openai.com>\n")
        (c,) = self.walk()
        self.assertEqual(uses_of(c)["OpenAI Codex"].vendor, "OpenAI")

    def test_aider_forms(self):
        self.r.commit({"a.py": "x\n"},
                      "a\n\nCo-authored-by: aider (anthropic/claude-sonnet-4) <aider@aider.chat>\n")
        self.r.commit({"b.py": "x\n"}, "b", author=("Human Dev (aider)", "human@example.com"))
        self.r.commit({"c.py": "x\n"}, "aider: c")
        # committer-only mark: aider committed the human's own edits -> not AI
        self.r.commit({"d.py": "x\n"}, "d", committer=("Human Dev (aider)", "human@example.com"))
        c1, c2, c3, c4 = self.walk()
        u = uses_of(c1)["Aider"]
        self.assertEqual(u.model, "anthropic/claude-sonnet-4")
        self.assertEqual(u.providers(), ["Anthropic"])
        self.assertIn("Aider", uses_of(c2))
        self.assertIn("Aider", uses_of(c3))
        self.assertEqual(c4.uses, [])

    def test_assisted_by(self):
        self.r.commit({"a.py": "x\n"}, "a\n\nAssisted-by: Gemini:gemini-2.5-pro\n")
        (c,) = self.walk()
        u = c.uses[0]
        self.assertEqual((u.name, u.model, u.model_provider), ("Gemini", "gemini-2.5-pro", "Google"))

    def test_human_commit_has_no_use(self):
        self.r.commit({"a.py": "x\n"},
                      "a\n\nCo-authored-by: Pat <pat@example.com>\nSigned-off-by: Pat <pat@example.com>\n")
        (c,) = self.walk()
        self.assertEqual(c.uses, [])
        self.assertEqual(c.basis, "none")


class TestLines(Base):
    def test_trailer_counts_all_added_lines(self):
        self.r.commit({"x.py": lines(3), "y.py": lines(2)},
                      "a\n\nCo-authored-by: Codex <noreply@openai.com>\n")
        self.r.commit({"z.py": lines(5)}, "human")
        files, _ = aiprov.aggregate(self.walk())
        self.assertEqual((files["x.py"]["ai_lines"], files["x.py"]["basis"]), (3, {"trailer"}))
        self.assertEqual(files["y.py"]["ai_lines"], 2)
        self.assertEqual(files["z.py"]["ai_lines"], 0)
        self.assertEqual(files["z.py"]["ai_commits"], [])

    def test_note_gives_exact_lines(self):
        sha = self.r.commit({"src/app.py": lines(10), "other.py": lines(4)}, "work")
        note = (
            "src/app.py\n"
            "  s_1a2b3c4d5e6f70::t_0a1b2c3d4e5f60 1-4,7\n"
            "  h_9f8e7d6c5b4a32 5-6\n"
            "---\n"
            + json.dumps({
                "schema_version": "authorship/3.0.0",
                "base_commit_sha": self.base,
                "prompts": {},
                "sessions": {"s_1a2b3c4d5e6f70": {
                    "agent_id": {"tool": "cursor", "id": "sess", "model": "gpt-5"}}},
                "humans": {"h_9f8e7d6c5b4a32": {"author": "Human Dev <human@example.com>"}},
            }, indent=2) + "\n")
        self.r.note(sha, note)
        (c,) = self.walk()
        self.assertEqual(c.basis, "note")
        u = uses_of(c)["Cursor"]
        self.assertEqual((u.model, u.model_provider, u.evidence), ("gpt-5", "OpenAI", "note"))
        files, _ = aiprov.aggregate([c])
        self.assertEqual(files["src/app.py"]["ai_lines"], 5)
        self.assertEqual(files["src/app.py"]["basis"], {"note"})
        self.assertEqual(files["other.py"]["ai_lines"], 0)
        self.assertEqual(files["other.py"]["ai_commits"], [])

    def test_legacy_prompt_key_and_trailer_together(self):
        sha = self.r.commit({"a.py": lines(6)},
                            "a\n\nCo-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>\n")
        note = ("a.py\n  0123456789abcdef 2-3\n---\n" + json.dumps({
            "schema_version": "authorship/3.0.0", "base_commit_sha": self.base,
            "prompts": {"0123456789abcdef": {
                "agent_id": {"tool": "claude", "id": "c", "model": "claude-sonnet-5"},
                "total_additions": 2, "total_deletions": 0, "accepted_lines": 2,
                "overriden_lines": 0}}}))
        self.r.note(sha, note)
        (c,) = self.walk()
        files, _ = aiprov.aggregate([c])
        self.assertEqual(files["a.py"]["ai_lines"], 2)  # note wins over the trailer's upper bound
        self.assertEqual({u.evidence for u in c.uses}, {"trailer", "note"})

    def test_rename_is_tracked(self):
        self.r.commit({"old.py": lines(5)}, "human")
        self.r.git("mv", "old.py", "new name.py")
        self.r.commit({"new name.py": lines(5) + "extra\n"},
                      "a\n\nCo-authored-by: Codex <noreply@openai.com>\n")
        files, _ = aiprov.aggregate(self.walk())
        self.assertEqual(files["new name.py"]["added"], 1)
        self.assertEqual(files["new name.py"]["ai_lines"], 1)


class TestPolicyAndOutput(Base):
    def setUp(self):
        super().setUp()
        self.c_claude = self.r.commit(
            {"a.py": lines(3)},
            f"feat {SECRET}\n\nsecret body {SECRET}\n\n"
            "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
            "Claude-Session: https://claude.ai/code/session_01ABC\n")
        self.c_cursor = self.r.commit({"b.py": lines(2)},
                                      f"{SECRET}\n\nCo-authored-by: Cursor <cursoragent@cursor.com>\n")
        self.r.commit({"human.txt": lines(2)}, f"human {SECRET}")
        self.r.git("tag", "v2")
        self.pol = os.path.join(self.r.path, ".git", "policy.json")

    def write_policy(self, obj):
        with open(self.pol, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def test_policy_pass(self):
        self.write_policy({"allowed_tools": ["Claude Code", "Cursor"],
                           "allowed_providers": ["Anthropic", "Anysphere"]})
        code, out, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                                 "--policy", self.pol, "--format", "md")
        self.assertEqual(code, 0, err)
        self.assertIn("Pass.", out)

    def test_policy_fail_lists_commits(self):
        self.write_policy({"allowed_tools": ["Claude Code"], "allowed_providers": ["Anthropic"]})
        code, out, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                                 "--policy", self.pol, "--format", "md")
        self.assertEqual(code, 1)
        self.assertIn(self.c_cursor, err)
        self.assertNotIn(self.c_claude, err)
        self.assertIn("tool 'Cursor' is not on the allowed list", err)
        self.assertIn("provider 'Anysphere' is not on the allowed list", err)
        self.assertIn("**Fail.**", out)

    def test_policy_unknown_provider(self):
        self.r.commit({"c.py": "x\n"}, "aider: c")  # aider, no model -> provider unknown
        self.write_policy({"allowed_tools": ["Claude Code", "Cursor", "Aider"],
                           "allowed_providers": ["Anthropic", "Anysphere"]})
        code, _, err = run_cli("--repo", self.r.path, "--since", "v1", "--policy", self.pol,
                               "--format", "md")
        self.assertEqual(code, 1)
        self.assertIn("provider unknown", err)
        self.write_policy({"allowed_tools": ["Claude Code", "Cursor", "Aider"],
                           "allowed_providers": ["Anthropic", "Anysphere"],
                           "allow_unknown_provider": True})
        code, _, _ = run_cli("--repo", self.r.path, "--since", "v1", "--policy", self.pol,
                             "--format", "md")
        self.assertEqual(code, 0)

    def test_cyclonedx_shape(self):
        code, out, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2")
        self.assertEqual(code, 0, err)
        bom = json.loads(out)
        for k in ("bomFormat", "specVersion", "serialNumber", "version", "metadata", "components"):
            self.assertIn(k, bom)
        self.assertEqual((bom["bomFormat"], bom["specVersion"]), ("CycloneDX", "1.6"))
        self.assertRegex(bom["serialNumber"], r"^urn:uuid:[0-9a-f-]{36}$")
        self.assertEqual(bom["metadata"]["component"]["version"], "v2")
        by_name = {c["name"]: {p["name"]: p["value"] for p in c["properties"]}
                   for c in bom["components"]}
        self.assertEqual(by_name["a.py"]["AI Generated"], "true")
        self.assertEqual(by_name["a.py"]["aiprov:ai.lines"], "3")
        self.assertEqual(by_name["b.py"]["AI Generated"], "true")
        self.assertNotIn("AI Generated", by_name["human.txt"])
        for c in bom["components"]:
            self.assertEqual(c["type"], "file")
            self.assertEqual(c["hashes"][0]["alg"], "SHA-256")
        svcs = {s["name"]: s for s in bom["formulation"][0]["services"]}
        self.assertEqual(svcs["Claude Code"]["provider"]["name"], "Anthropic")
        props = {p["name"]: p["value"] for p in svcs["Claude Code"]["properties"]}
        self.assertEqual(props["aiprov:model"], "Claude Opus 5.5")
        self.assertEqual(svcs["Cursor"]["provider"]["name"], "Anysphere")
        models = [m for m in bom["formulation"][0]["components"]]
        self.assertEqual(models[0]["type"], "machine-learning-model")
        self.assertNotIn(SECRET, out)

    def test_spdx_shape(self):
        code, out, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                                 "--format", "spdx")
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["@context"], "https://spdx.org/rdf/3.0.1/spdx-context.jsonld")
        graph = doc["@graph"]
        types = [g["type"] for g in graph]
        for t in ("CreationInfo", "SpdxDocument", "software_Package", "software_File",
                  "Tool", "Relationship", "Annotation", "Organization"):
            self.assertIn(t, types)
        ci = next(g for g in graph if g["type"] == "CreationInfo")
        self.assertEqual(ci["specVersion"], "3.0.1")
        for k in ("created", "createdBy"):
            self.assertIn(k, ci)
        ids = set()
        for g in graph:
            if g["type"] == "CreationInfo":
                continue
            self.assertIn("spdxId", g)
            self.assertEqual(g["creationInfo"], ci["@id"])
            ids.add(g["spdxId"])
        rel_types = {g["relationshipType"] for g in graph if g["type"] == "Relationship"}
        self.assertTrue({"usesTool", "generates", "contains", "availableFrom"} <= rel_types)
        for g in graph:
            if g["type"] == "Relationship":
                self.assertIn(g["from"], ids)
                for t in g["to"]:
                    self.assertIn(t, ids)
        anns = [g for g in graph if g["type"] == "Annotation"]
        self.assertEqual(len(anns), 2)
        self.assertTrue(all(a["statement"].startswith("AI Generated") for a in anns))
        doc_el = next(g for g in graph if g["type"] == "SpdxDocument")
        self.assertEqual(set(doc_el["element"]), ids - {doc_el["spdxId"]})
        self.assertNotIn(SECRET, out)

    def test_markdown_and_privacy(self):
        code, out, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                                 "--format", "md")
        self.assertEqual(code, 0, err)
        self.assertIn("| Claude Code | Claude Opus 5.5 | Anthropic |", out)
        self.assertIn("`a.py`", out)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("human@example.com", out)

    def test_bad_ref_is_usage_error(self):
        code, _, err = run_cli("--repo", self.r.path, "--since", "no-such-tag")
        self.assertEqual(code, 2)
        self.assertIn("failed", err)

    def test_root_range(self):
        code, out, _ = run_cli("--repo", self.r.path, "--since", "ROOT", "--format", "md")
        self.assertEqual(code, 0)
        self.assertIn("Commits: 4", out)


REVIEW_LINES = [
    ("Gemini Code Assist", "Google",
     "Co-authored-by: gemini-code-assist[bot] <176961590+gemini-code-assist[bot]@users.noreply.github.com>"),
    ("Gemini Code Assist", "Google",
     "Co-authored-by: gemini-code-assist[bot] <gemini-code-assist[bot]@users.noreply.github.com>"),
    ("Gemini Code Assist", "Google",
     "Co-authored-by: Antigravity <gemini-code-assist[bot]@users.noreply.github.com>"),
    ("CodeRabbit", "CodeRabbit",
     "Co-authored-by: coderabbitai[bot] <136622811+coderabbitai[bot]@users.noreply.github.com>"),
    ("CodeRabbit", "CodeRabbit",
     "Co-authored-by: coderabbiteu[bot] <274295283+coderabbiteu[bot]@users.noreply.github.com>"),
    ("GitHub Copilot code review", "GitHub",
     "Co-authored-by: copilot-pull-request-reviewer[bot] "
     "<175728472+copilot-pull-request-reviewer[bot]@users.noreply.github.com>"),
    ("GitHub Copilot code review", "GitHub",
     "Co-authored-by: copilot-pull-request-reviewer <copilot-pull-request-reviewer@users.noreply.github.com>"),
    ("GitHub Copilot code review", "GitHub",
     "Co-authored-by: Copilot <copilot-pull-request-reviewer@github.com>"),
    ("GitHub Copilot code review", "GitHub",
     "Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>"),
    ("Sourcery", "Sourcery",
     "Co-authored-by: sourcery-ai[bot] <58596630+sourcery-ai[bot]@users.noreply.github.com>"),
    ("Qodo code review", "Qodo",
     "Co-authored-by: qodo-code-review[bot] <151058649+qodo-code-review[bot]@users.noreply.github.com>"),
    ("Greptile", "Greptile",
     "Co-authored-by: greptile-apps[bot] <165735046+greptile-apps[bot]@users.noreply.github.com>"),
    ("cubic", "cubic",
     "Co-authored-by: cubic-dev-ai[bot] <191113872+cubic-dev-ai[bot]@users.noreply.github.com>"),
    ("OpenAI Codex code review", "OpenAI",
     "Co-authored-by: chatgpt-codex-connector[bot] "
     "<199175422+chatgpt-codex-connector[bot]@users.noreply.github.com>"),
    ("OpenAI Codex code review", "OpenAI",
     "Co-authored-by: Codex (some-model) <199175422+chatgpt-codex-connector[bot]@users.noreply.github.com>"),
]


class TestReviewBots(Base):
    def test_each_bot_line_is_detected(self):
        for name, vendor, line in REVIEW_LINES:
            with self.subTest(line=line):
                self.r.commit({"f.py": line + "\n"}, f"apply suggestion {SECRET}\n\n{line}\n")
                c = self.walk()[-1]
                self.assertEqual([u.name for u in c.uses], [name])
                u = c.uses[0]
                self.assertEqual(u.kind, aiprov.KIND_REVIEW)
                self.assertEqual(u.vendor, vendor)
                self.assertEqual(u.evidence, "trailer")
                self.assertEqual(u.traces, [line])
                self.assertIsNone(u.model)

    def test_not_a_review_bot(self):
        self.r.commit({"a.py": "x\n"}, "a\n\nCo-authored-by: coderabbitai[bot] <someone@example.com>\n")
        self.r.commit({"b.py": "x\n"}, "b\n\nCo-authored-by: gemini-code-assist[bot] "
                                       "<gemini-code-assist[bot]@users.noreply.example.com>\n")
        self.r.commit({"c.py": "x\n"}, "c\n\nCo-authored-by: dependabot[bot] "
                                       "<49699333+dependabot[bot]@users.noreply.github.com>\n")
        self.r.commit({"d.py": "x\n"}, "d\n\nCo-authored-by: Real Person "
                                       "<5+greptile-apps-fan@users.noreply.github.com>\n")
        self.assertEqual([c.uses for c in self.walk()], [[], [], [], []])

    def test_copilot_reviewer_id_is_not_the_coding_agent(self):
        self.r.commit({"a.py": "x\n"}, "a\n\nCo-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>\n")
        self.r.commit({"b.py": "x\n"}, "b\n\nCo-authored-by: copilot-swe-agent[bot] "
                                       "<198982749+Copilot@users.noreply.github.com>\n")
        c1, c2 = self.walk()
        self.assertEqual([u.name for u in c1.uses], ["GitHub Copilot code review"])
        self.assertEqual([u.name for u in c2.uses], ["GitHub Copilot coding agent"])
        self.assertEqual(c2.uses[0].kind, aiprov.KIND_CODING)

    def test_repeated_squash_lines_make_one_use(self):
        line = ("Co-authored-by: gemini-code-assist[bot] "
                "<176961590+gemini-code-assist[bot]@users.noreply.github.com>")
        self.r.commit({"a.py": lines(4)}, f"Squash\n\n* one\n\n{line}\n\n* two\n\n{line}\n\n{line}\n")
        (c,) = self.walk()
        self.assertEqual(len(c.uses), 1)
        self.assertEqual(c.uses[0].traces, [line])

    def test_review_bot_does_not_add_ai_lines(self):
        self.r.commit({"a.py": lines(4)},
                      "apply\n\nCo-authored-by: coderabbitai[bot] "
                      "<136622811+coderabbitai[bot]@users.noreply.github.com>\n")
        (c,) = self.walk()
        self.assertEqual(c.basis, "none")
        self.assertEqual(c.ai_lines, {})
        self.assertEqual(c.review_lines, {"a.py": 4})
        files, tools = aiprov.aggregate([c])
        self.assertEqual(files["a.py"]["ai_lines"], 0)
        self.assertEqual(files["a.py"]["ai_commits"], [])
        self.assertEqual(files["a.py"]["review_lines"], 4)
        self.assertEqual(files["a.py"]["review_tools"], {"CodeRabbit"})

    def test_mixed_commit_keeps_both_kinds_apart(self):
        self.r.commit({"a.py": lines(5)},
                      "feat\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
                      "Co-authored-by: sourcery-ai[bot] <58596630+sourcery-ai[bot]@users.noreply.github.com>\n")
        (c,) = self.walk()
        self.assertEqual({u.name: u.kind for u in c.uses},
                         {"Claude Code": aiprov.KIND_CODING, "Sourcery": aiprov.KIND_REVIEW})
        files, _ = aiprov.aggregate([c])
        e = files["a.py"]
        self.assertEqual(e["tools"], {"Claude Code"})
        self.assertEqual(e["review_tools"], {"Sourcery"})
        self.assertEqual((e["ai_lines"], e["review_lines"]), (5, 5))

    def _setup_outputs(self):
        self.r.commit({"a.py": lines(3)},
                      f"feat {SECRET}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n")
        self.r.commit({"b.py": lines(2)},
                      f"apply {SECRET}\n\nCo-authored-by: gemini-code-assist[bot] "
                      "<176961590+gemini-code-assist[bot]@users.noreply.github.com>\n")
        self.r.git("tag", "v2")

    def test_cyclonedx_separates_kinds(self):
        self._setup_outputs()
        code, out, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2")
        self.assertEqual(code, 0, err)
        bom = json.loads(out)
        svcs = {s["name"]: {p["name"]: p["value"] for p in s["properties"]}
                for s in bom["formulation"][0]["services"]}
        self.assertEqual(svcs["Claude Code"]["aiprov:kind"], aiprov.KIND_CODING)
        self.assertEqual(svcs["Gemini Code Assist"]["aiprov:kind"], aiprov.KIND_REVIEW)
        by_name = {c["name"]: {p["name"]: p["value"] for p in c["properties"]}
                   for c in bom["components"]}
        self.assertEqual(by_name["a.py"]["AI Generated"], "true")
        self.assertNotIn("aiprov:review.lines", by_name["a.py"])
        self.assertNotIn("AI Generated", by_name["b.py"])
        self.assertEqual(by_name["b.py"]["aiprov:review.lines"], "2")
        self.assertEqual(by_name["b.py"]["aiprov:review.bots"], "Gemini Code Assist")
        rel = {p["name"]: p["value"] for p in bom["metadata"]["component"]["properties"]}
        self.assertEqual((rel["aiprov:commits.ai"], rel["aiprov:commits.review"]), ("1", "1"))
        self.assertEqual((rel["aiprov:files.ai"], rel["aiprov:files.review"]), ("1", "1"))
        self.assertEqual(rel["AI Generated"], "true")
        props = [p for p in bom["formulation"][0]["properties"] if p["name"] == "aiprov:review.commit"]
        self.assertEqual(len(props), 1)
        self.assertNotIn(SECRET, out)

    def test_cyclonedx_review_only_is_not_ai_generated(self):
        self.r.commit({"b.py": lines(2)}, "apply\n\nCo-authored-by: coderabbitai[bot] "
                      "<136622811+coderabbitai[bot]@users.noreply.github.com>\n")
        _, out, _ = run_cli("--repo", self.r.path, "--since", "v1")
        bom = json.loads(out)
        rel = {p["name"]: p["value"] for p in bom["metadata"]["component"]["properties"]}
        self.assertEqual(rel["AI Generated"], "false")

    def test_spdx_separates_kinds(self):
        self._setup_outputs()
        _, out, _ = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                            "--format", "spdx")
        graph = json.loads(out)["@graph"]
        tools = {g["name"]: g for g in graph if g["type"] == "Tool" and "summary" in g}
        self.assertTrue(tools["Gemini Code Assist"]["summary"].startswith(aiprov.KIND_REVIEW))
        self.assertEqual(tools["Gemini Code Assist"]["comment"], f"aiprov:kind={aiprov.KIND_REVIEW}")
        self.assertTrue(tools["Claude Code (Claude Opus 5.5)"]["summary"].startswith(aiprov.KIND_CODING))
        gen = [g for g in graph if g["type"] == "Relationship" and g["relationshipType"] == "generates"]
        self.assertEqual(len(gen), 1)  # only the coding tool
        self.assertEqual(gen[0]["from"], tools["Claude Code (Claude Opus 5.5)"]["spdxId"])
        anns = [g["statement"] for g in graph if g["type"] == "Annotation"]
        self.assertEqual(len([a for a in anns if a.startswith("AI Generated")]), 1)
        self.assertEqual(len([a for a in anns if a.startswith("AI review bot suggestion")]), 1)
        ids = {g["spdxId"] for g in graph if "spdxId" in g}
        doc = next(g for g in graph if g["type"] == "SpdxDocument")
        self.assertEqual(set(doc["element"]), ids - {doc["spdxId"]})
        self.assertNotIn(SECRET, out)

    def test_markdown_has_its_own_section(self):
        self._setup_outputs()
        _, out, _ = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                            "--format", "md")
        head, _, tail = out.partition("## AI review bots (applied suggestions)")
        self.assertTrue(tail)
        self.assertIn("| Claude Code | Claude Opus 5.5 |", head)
        self.assertNotIn("Gemini Code Assist", head)
        self.assertNotIn("`b.py`", head)
        self.assertIn("| Gemini Code Assist | Google | 1 | trailer |", tail)
        self.assertIn("`b.py`", tail)
        self.assertIn("Lines added: 5. Counted as AI: 3.", out)
        self.assertNotIn(SECRET, out)

    def test_markdown_mixed_commit_bot_not_in_coding_table(self):
        self.r.commit({"a.py": lines(5)},
                      "feat\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
                      "Co-authored-by: sourcery-ai[bot] <58596630+sourcery-ai[bot]@users.noreply.github.com>\n")
        _, out, _ = run_cli("--repo", self.r.path, "--since", "v1", "--format", "md")
        head, _, tail = out.partition("## AI review bots (applied suggestions)")
        self.assertNotIn("Sourcery", head)
        self.assertIn("| Sourcery |", tail)
        self.assertIn("With an AI coding tool trace: 1.", out)

    def test_markdown_none_when_no_review_bot(self):
        self.r.commit({"a.py": "x\n"}, "h")
        _, out, _ = run_cli("--repo", self.r.path, "--since", "v1", "--format", "md")
        self.assertIn("## AI review bots (applied suggestions)", out)
        self.assertIn("With an AI review bot as co-author (counted apart, see below): 0.", out)

    def test_policy_applies_to_review_bots(self):
        self._setup_outputs()
        pol = os.path.join(self.r.path, ".git", "policy.json")

        def write(obj):
            with open(pol, "w", encoding="utf-8") as fh:
                json.dump(obj, fh)

        write({"allowed_tools": ["Claude Code"], "allowed_providers": ["Anthropic"]})
        code, out, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                                 "--policy", pol, "--format", "md")
        self.assertEqual(code, 1)
        self.assertIn("Gemini Code Assist", err)
        self.assertIn(aiprov.KIND_REVIEW, err)
        self.assertIn("tool 'Gemini Code Assist' is not on the allowed list", err)
        self.assertIn("provider 'Google' is not on the allowed list", err)
        self.assertIn("**Fail.**", out)
        write({"allowed_tools": ["Claude Code", "review:gemini-code-assist"],
               "allowed_providers": ["Anthropic", "Google"]})
        code, out, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                                 "--policy", pol, "--format", "md")
        self.assertEqual(code, 0, err)
        self.assertIn("Pass.", out)
        write({"allowed_tools": ["Claude Code", "Gemini Code Assist"],
               "allowed_providers": ["Anthropic", "Google"]})
        code, _, err = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                               "--policy", pol, "--format", "md")
        self.assertEqual(code, 0, err)


SCHEMA_DIR = os.environ.get("AIPROV_SCHEMA_DIR")
try:
    import jsonschema  # noqa: F401  (optional, not needed by aiprov itself)
    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False


@unittest.skipUnless(SCHEMA_DIR and HAVE_JSONSCHEMA, "set AIPROV_SCHEMA_DIR and install jsonschema")
class TestOfficialSchemas(TestPolicyAndOutput):
    def _load(self, name):
        with open(os.path.join(SCHEMA_DIR, name), encoding="utf-8") as fh:
            return json.load(fh)

    def test_cyclonedx_schema(self):
        import jsonschema
        from referencing import Registry, Resource
        main = self._load("bom-1.6.schema.json")
        reg = Registry()
        for extra in ("spdx.schema.json", "jsf-0.82.schema.json"):
            res = Resource.from_contents(self._load(extra))
            reg = reg.with_resource(extra, res).with_resource(
                "http://cyclonedx.org/schema/" + extra, res)
        _, out, _ = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2")
        jsonschema.Draft7Validator(main, registry=reg).validate(json.loads(out))

    def test_spdx_schema(self):
        import jsonschema
        schema = self._load("spdx-3.0.1-schema.json")
        _, out, _ = run_cli("--repo", self.r.path, "--since", "v1", "--until", "v2",
                            "--format", "spdx")
        jsonschema.validators.validator_for(schema)(schema).validate(json.loads(out))


CLAUDE_STAMP = "Claude-Session: https://claude.ai/code/session_01ABC\n"
CURSOR_STAMP = "Co-authored-by: Cursor <cursoragent@cursor.com>\n"


class PushBase(unittest.TestCase):
    """A work repo, bare repos as remotes, and real `git push` runs."""

    def setUp(self):
        self.r = Repo()
        self.r.commit({"README": "base\n"}, "base")
        self.tmp = tempfile.mkdtemp(prefix="aiprov-remote-")
        self.pol = os.path.join(self.tmp, "policy.json")  # outside the work repo
        self.remotes = {}
        self.add_remote("client", "clientco-remote")

    def tearDown(self):
        self.r.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_remote(self, name, dirname):
        path = os.path.join(self.tmp, dirname + ".git")
        subprocess.run(["git", "init", "--bare", "-q", path], check=True, env=self.r.env)
        self.r.git("remote", "add", name, path)
        self.remotes[name] = path

    def policy(self, **obj):
        with open(self.pol, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def install(self, *extra):
        return run_cli("hook-install", "--policy", self.pol, "--repo", self.r.path, *extra)

    def push(self, *args):
        p = subprocess.run(["git", "-C", self.r.path, "push", *args], capture_output=True,
                           env=self.r.env)
        return p.returncode, p.stderr.decode("utf-8", "replace")

    def remote_refs(self, name="client"):
        p = subprocess.run(["git", "-C", self.remotes[name], "for-each-ref", "--format=%(refname)"],
                           capture_output=True, env=self.r.env)
        return p.stdout.decode().split()

    def log_lines(self):
        path = os.path.join(self.r.path, ".git", "aiprov-log.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(x) for x in fh.read().splitlines()]

    def stamped(self, stamp, name):
        return self.r.commit({name: lines(2)}, f"work {SECRET}\n\nbody {SECRET}\n\n{stamp}")


class TestPushHook(PushBase):
    def test_blocks_disallowed_tool_and_lists_commits(self):
        self.policy(allowed_tools=["Claude Code"], allowed_providers=["Anthropic"])
        self.assertEqual(self.install()[0], 0)
        bad = self.stamped(CURSOR_STAMP, "a.py")
        code, err = self.push("client", "main")
        self.assertNotEqual(code, 0)
        self.assertIn("REFUSED", err)
        self.assertIn(bad[:12], err)
        self.assertIn("Cursor", err)
        self.assertNotIn(SECRET, err)
        self.assertEqual(self.remote_refs(), [])  # nothing reached the client

    def test_allows_allowed_and_human_commits(self):
        self.policy(allowed_tools=["Claude Code", "Cursor"])
        self.install()
        self.stamped(CLAUDE_STAMP, "a.py")
        self.r.commit({"h.txt": "x\n"}, "human only")
        code, err = self.push("client", "main")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.remote_refs(), ["refs/heads/main"])

    def test_only_new_commits_are_checked(self):
        self.policy(allowed_tools=["Claude Code"])
        self.install()
        first = self.stamped(CLAUDE_STAMP, "a.py")
        self.assertEqual(self.push("client", "main")[0], 0)
        second = self.stamped(CURSOR_STAMP, "b.py")
        code, err = self.push("client", "main")
        self.assertNotEqual(code, 0)
        self.assertIn(second[:12], err)
        self.assertNotIn(first[:12], err)

    def test_new_branch_only_checks_what_the_remote_lacks(self):
        self.policy(allowed_tools=["Claude Code"])
        self.install()
        self.stamped(CLAUDE_STAMP, "a.py")
        self.assertEqual(self.push("client", "main")[0], 0)
        self.r.git("checkout", "-q", "-b", "feature")
        bad = self.stamped(CURSOR_STAMP, "b.py")
        code, err = self.push("client", "feature")
        self.assertNotEqual(code, 0)
        self.assertEqual(err.count("REFUSED"), 1)
        self.assertIn(bad[:12], err)
        self.assertEqual(self.remote_refs(), ["refs/heads/main"])

    def test_deleting_a_branch_is_allowed(self):
        self.policy(allowed_tools=["Claude Code"])
        self.install()
        self.r.git("checkout", "-q", "-b", "gone")
        self.assertEqual(self.push("client", "gone")[0], 0)
        self.r.git("checkout", "-q", "main")
        code, err = self.push("client", ":gone")
        self.assertEqual(code, 0, err)

    def test_remote_pattern_only_guards_matching_remotes(self):
        self.add_remote("personal", "mine")
        self.policy(allowed_tools=["Claude Code"])
        self.assertEqual(self.install("--remote", "client")[0], 0)
        self.stamped(CURSOR_STAMP, "a.py")
        self.assertEqual(self.push("personal", "main")[0], 0)  # not guarded
        self.assertNotEqual(self.push("client", "main")[0], 0)  # guarded
        self.assertEqual(self.remote_refs("client"), [])
        self.assertEqual(self.remote_refs("personal"), ["refs/heads/main"])
        result = [(e["remote"], e["result"]) for e in self.log_lines()]
        self.assertEqual(result, [("personal", "skipped"), ("client", "blocked")])

    def test_remote_pattern_can_match_the_url(self):
        self.add_remote("personal", "mine")
        self.policy(allowed_tools=["Claude Code"])
        self.install("--remote", "*clientco*")  # glob on the URL
        self.stamped(CURSOR_STAMP, "a.py")
        self.assertEqual(self.push("personal", "main")[0], 0)
        self.assertNotEqual(self.push("client", "main")[0], 0)

    def test_plain_word_matches_name_exactly_or_url_part(self):
        self.assertTrue(aiprov.remote_matches(["client"], "client", "/x/y"))
        self.assertFalse(aiprov.remote_matches(["client"], "client2", "/x/y"))
        self.assertTrue(aiprov.remote_matches(["acme"], "origin", "https://git.acme.com/r.git"))
        self.assertTrue(aiprov.remote_matches(["*.acme.com/*"], "o", "https://git.acme.com/r.git"))
        self.assertFalse(aiprov.remote_matches(["acme"], "origin", "https://github.com/me/r.git"))
        self.assertTrue(aiprov.remote_matches([], "anything", None))

    def test_ban_all_ai(self):
        self.policy(allowed_tools=["Claude Code", "Cursor"], ban_all_ai=True)
        self.install()
        self.r.commit({"h.txt": "x\n"}, "human only")
        self.assertEqual(self.push("client", "main")[0], 0)  # unstamped is fine
        bad = self.stamped(CLAUDE_STAMP, "a.py")  # on the allowed list, still banned
        code, err = self.push("client", "main")
        self.assertNotEqual(code, 0)
        self.assertIn(bad[:12], err)
        self.assertIn("banned", err)

    def test_ban_all_ai_also_fails_the_report_check(self):
        self.policy(allowed_tools=["Claude Code"], ban_all_ai=True)
        self.r.git("tag", "v0", "HEAD")
        self.stamped(CLAUDE_STAMP, "a.py")
        code, _, err = run_cli("--repo", self.r.path, "--since", "v0", "--policy", self.pol,
                               "--format", "md")
        self.assertEqual(code, 1)
        self.assertIn("banned", err)

    def test_bad_policy_values_are_refused(self):
        self.policy(ban_all_ai="true")
        code, _, err = run_cli("check", "--policy", self.pol, "--repo", self.r.path,
                               "--range", "HEAD")
        self.assertEqual(code, 2)
        self.assertIn("ban_all_ai", err)
        self.assertEqual(self.install()[0], 2)

    def test_stamped_merge_commit_is_seen(self):
        self.policy(allowed_tools=[], ban_all_ai=True)
        self.r.git("checkout", "-q", "-b", "side")
        self.r.commit({"s.txt": "s\n"}, "side work")
        self.r.git("checkout", "-q", "main")
        self.r.commit({"m.txt": "m\n"}, "main work")
        self.r.git("merge", "--no-ff", "-q", "-m", "Merge side\n\n" + CLAUDE_STAMP, "side")
        merge = self.r.git("rev-parse", "HEAD")
        code, _, err = run_cli("check", "--policy", self.pol, "--repo", self.r.path,
                               "--range", "HEAD~2..HEAD")
        self.assertEqual(code, 1)
        self.assertIn(merge[:12], err)

    def test_fails_closed_when_policy_is_gone(self):
        self.policy(allowed_tools=["Claude Code"])
        self.install()
        os.remove(self.pol)
        self.stamped(CLAUDE_STAMP, "a.py")
        code, err = self.push("client", "main")
        self.assertNotEqual(code, 0)
        self.assertIn("cannot read policy", err)
        self.assertEqual(self.remote_refs(), [])

    def test_hook_outside_repo_is_not_installed(self):
        self.policy(allowed_tools=[])
        shared = os.path.join(self.tmp, "shared-hooks")
        os.makedirs(shared)
        self.r.git("config", "core.hooksPath", shared)
        code, _, err = self.install()
        self.assertEqual(code, 2)
        self.assertIn("outside this repo", err)
        self.assertEqual(os.listdir(shared), [])


class TestHookChain(PushBase):
    def hook_path(self, name="pre-push"):
        return os.path.join(self.r.path, ".git", "hooks", name)

    def write_old_hook(self, exit_code=0):
        marker = os.path.join(self.tmp, "old-hook-ran")
        os.makedirs(os.path.dirname(self.hook_path()), exist_ok=True)
        with open(self.hook_path(), "w", newline="\n") as fh:
            fh.write("#!/bin/sh\ncat > /dev/null\n"
                     f"echo \"$1\" > '{marker.replace(os.sep, '/')}'\n"
                     f"exit {exit_code}\n")
        os.chmod(self.hook_path(), 0o755)
        return marker

    def test_existing_hook_is_kept_and_runs_after_the_check(self):
        marker = self.write_old_hook()
        self.policy(allowed_tools=["Claude Code"])
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn("pre-push.pre-aiprov", out)
        self.assertTrue(os.path.exists(self.hook_path("pre-push.pre-aiprov")))
        self.stamped(CLAUDE_STAMP, "a.py")
        code, err = self.push("client", "main")
        self.assertEqual(code, 0, err)
        with open(marker) as fh:
            self.assertEqual(fh.read().strip(), "client")  # it got git's arguments

    def test_blocked_push_does_not_reach_the_old_hook(self):
        marker = self.write_old_hook()
        self.policy(allowed_tools=["Claude Code"])
        self.install()
        self.stamped(CURSOR_STAMP, "a.py")
        self.assertNotEqual(self.push("client", "main")[0], 0)
        self.assertFalse(os.path.exists(marker))

    def test_failing_old_hook_still_blocks(self):
        self.write_old_hook(exit_code=1)
        self.policy(allowed_tools=["Claude Code"])
        self.install()
        self.stamped(CLAUDE_STAMP, "a.py")
        self.assertNotEqual(self.push("client", "main")[0], 0)
        self.assertEqual(self.remote_refs(), [])

    def test_install_twice_keeps_the_old_hook(self):
        self.write_old_hook()
        self.policy(allowed_tools=["Claude Code"])
        self.install()
        with open(self.hook_path("pre-push.pre-aiprov")) as fh:
            old = fh.read()
        self.assertEqual(self.install("--remote", "client")[0], 0)
        with open(self.hook_path("pre-push.pre-aiprov")) as fh:
            self.assertEqual(fh.read(), old)
        with open(self.hook_path()) as fh:
            self.assertIn("--remote 'client'", fh.read())

    def test_two_unknown_hooks_are_not_overwritten(self):
        self.write_old_hook()
        with open(self.hook_path("pre-push.pre-aiprov"), "w", newline="\n") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        with open(self.hook_path()) as fh:
            before = fh.read()
        self.policy(allowed_tools=[])
        code, _, err = self.install()
        self.assertEqual(code, 2)
        self.assertIn("hook-run", err)
        with open(self.hook_path()) as fh:
            self.assertEqual(fh.read(), before)


class TestCheckAndLog(PushBase):
    def test_check_range_for_ci(self):
        self.policy(allowed_tools=["Claude Code"])
        base = self.r.git("rev-parse", "HEAD")
        ok = self.stamped(CLAUDE_STAMP, "a.py")
        code, _, err = run_cli("check", "--policy", self.pol, "--repo", self.r.path,
                               "--range", f"{base}..{ok}")
        self.assertEqual(code, 0, err)
        bad = self.stamped(CURSOR_STAMP, "b.py")
        code, _, err = run_cli("check", "--policy", self.pol, "--repo", self.r.path,
                               "--range", f"{base}..{bad}")
        self.assertEqual(code, 1)
        self.assertIn(bad[:12], err)
        self.assertNotIn(SECRET, err)

    def test_check_bad_range_is_an_error_not_a_pass(self):
        self.policy(allowed_tools=[])
        for rng in ("nope..HEAD", "--all", "-n1"):
            code, _, err = run_cli("check", "--policy", self.pol, "--repo", self.r.path,
                                   "--range=" + rng)
            self.assertEqual(code, 2, rng)

    def test_log_is_append_only_and_has_no_commit_text(self):
        self.policy(allowed_tools=["Claude Code"])
        self.install()
        ok = self.stamped(CLAUDE_STAMP, "a.py")
        self.assertEqual(self.push("client", "main")[0], 0)
        bad = self.stamped(CURSOR_STAMP, "b.py")
        self.assertNotEqual(self.push("client", "main")[0], 0)
        run_cli("check", "--policy", self.pol, "--repo", self.r.path, "--range", f"{ok}..{bad}")
        entries = self.log_lines()
        self.assertEqual([e["result"] for e in entries], ["allowed", "blocked", "blocked"])
        first, second, third = entries
        self.assertEqual(first["offending_commits"], [])
        self.assertEqual(first["remote"], "client")
        self.assertEqual(first["via"], "hook")
        self.assertEqual(second["offending_commits"], [bad])
        self.assertEqual(second["range"], f"{ok}..{bad}")
        self.assertEqual(second["commits_checked"], 1)
        self.assertEqual(third["via"], "check")
        self.assertIsNone(third["remote"])
        self.assertTrue(second["time"].endswith("Z"))
        with open(os.path.join(self.r.path, ".git", "aiprov-log.jsonl"), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("Cursor <", raw)
        self.assertNotIn("claude.ai", raw)

    def test_log_does_not_keep_url_passwords(self):
        self.assertEqual(aiprov.clean_url("https://bob:s3cret@git.example.com/r.git"),
                         "https://git.example.com/r.git")
        self.assertEqual(aiprov.clean_url("git@example.com:r.git"), "git@example.com:r.git")
        self.assertIsNone(aiprov.clean_url(None))

    def test_unwritable_log_only_warns(self):
        self.policy(allowed_tools=["Claude Code"])
        os.makedirs(os.path.join(self.r.path, ".git", "aiprov-log.jsonl"))  # a folder in the way
        code, _, err = run_cli("check", "--policy", self.pol, "--repo", self.r.path,
                               "--range", "HEAD")
        self.assertEqual(code, 0)
        self.assertIn("could not write the log", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
