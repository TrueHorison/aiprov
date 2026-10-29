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


if __name__ == "__main__":
    unittest.main(verbosity=2)
