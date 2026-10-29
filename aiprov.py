#!/usr/bin/env python3
"""aiprov: list the AI coding tools that left a trace in a git range.

Reads commit trailers and git-ai notes (refs/notes/ai) between two refs and
writes a CycloneDX 1.6 SBOM, an SPDX 3.0.1 document, or a Markdown summary.
Optionally checks the AI use against an allow-list (policy.json).

It keeps two kinds apart: AI coding tools, and AI review bots whose suggestions were
applied (a "Co-authored-by" line from a bot such as gemini-code-assist[bot]).

Stdlib only. Calls git through subprocess. Never outputs commit message
text other than the AI trailer lines it recognises, and never outputs diffs.

Exit codes: 0 = ok, 1 = policy check failed, 2 = usage or git error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field

VERSION = "0.2.0"

# What kind of AI use a finding is. The two kinds are kept apart in every output.
KIND_CODING = "AI coding tool"
KIND_REVIEW = "AI review bot (applied suggestion)"

# --------------------------------------------------------------------------
# Known tools. key -> (display name, tool vendor or None)
# --------------------------------------------------------------------------
TOOLS = {
    "claude-code": ("Claude Code", "Anthropic"),
    "claude-github": ("Claude agent on GitHub", "Anthropic"),
    "copilot-agent": ("GitHub Copilot coding agent", "GitHub"),
    "github-agent": ("GitHub cloud agent (unnamed)", "GitHub"),
    "cursor": ("Cursor", "Anysphere"),
    "codex": ("OpenAI Codex", "OpenAI"),
    "aider": ("Aider", None),  # open source, runs locally; provider = model's
}

# AI review bots. A bot like this posts a code suggestion on a pull request. When a
# person clicks "Commit suggestion", GitHub adds the bot as a co-author of that commit.
# The bot did not write the commit: it wrote the suggested lines inside it.
# key -> (display name, vendor). The key starts with "review:".
REVIEW_BOTS = {
    "review:gemini-code-assist": ("Gemini Code Assist", "Google"),
    "review:coderabbit": ("CodeRabbit", "CodeRabbit"),
    "review:copilot-review": ("GitHub Copilot code review", "GitHub"),
    "review:sourcery": ("Sourcery", "Sourcery"),
    "review:qodo": ("Qodo code review", "Qodo"),
    "review:greptile": ("Greptile", "Greptile"),
    "review:cubic": ("cubic", "cubic"),
    "review:codex-review": ("OpenAI Codex code review", "OpenAI"),
}

# The bot's account name (the part of its noreply email before the "@", without any
# leading "<id>+" and without a trailing "[bot]") -> key in REVIEW_BOTS.
# The email decides, not the display name: the same account shows up under other
# names (for example "Antigravity" or "Codex (<model>)").
REVIEW_BOT_LOGINS = {
    "gemini-code-assist": "review:gemini-code-assist",
    "coderabbitai": "review:coderabbit",
    "coderabbiteu": "review:coderabbit",
    "copilot-pull-request-reviewer": "review:copilot-review",
    "sourcery-ai": "review:sourcery",
    "qodo-code-review": "review:qodo",
    "greptile-apps": "review:greptile",
    "cubic-dev-ai": "review:cubic",
    "chatgpt-codex-connector": "review:codex-review",
}
REVIEW_EMAIL_DOMAINS = {"users.noreply.github.com", "github.com"}
# GitHub account id of the Copilot code review bot. Its commits and co-author lines
# show the name "Copilot", which looks like the Copilot coding agent (id 198982749).
COPILOT_REVIEW_ID = "175728472"

# git-ai agent_id.tool values -> our tool keys. Unknown values pass through.
GITAI_TOOL_MAP = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claude_code": "claude-code",
    "cursor": "cursor",
    "codex": "codex",
    "github-copilot": "copilot-agent",
    "copilot": "copilot-agent",
    "aider": "aider",
}

# Model provider from an explicit "provider/model" prefix.
PREFIX_PROVIDERS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "azure": "Microsoft Azure",
    "bedrock": "Amazon Bedrock",
    "vertex_ai": "Google",
    "gemini": "Google",
    "google": "Google",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "mistral": "Mistral AI",
    "xai": "xAI",
    "ollama": "Local (Ollama)",
}

# Model provider from the start of a model name. Model names are fixed
# product names, so this is a lookup, not a guess about whether AI was used.
NAME_PROVIDERS = [
    (re.compile(r"^claude\b", re.I), "Anthropic"),
    (re.compile(r"^(gpt|o[1-9]\b|o[1-9]-|codex)", re.I), "OpenAI"),
    (re.compile(r"^gemini\b", re.I), "Google"),
]

COPILOT_EMAIL = re.compile(r"^(?!" + COPILOT_REVIEW_ID + r"\+)\d+\+Copilot@users\.noreply\.github\.com$", re.I)
COPILOT_REVIEW_EMAIL = re.compile(r"^" + COPILOT_REVIEW_ID + r"\+Copilot@users\.noreply\.github\.com$", re.I)
CLAUDE_GH_EMAIL = re.compile(r"^\d+\+Claude@users\.noreply\.github\.com$", re.I)
TRAILER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):[ \t]+(\S.*?)\s*$")
NAME_EMAIL = re.compile(r"^(.*?)\s*<([^>]*)>\s*$")
AIDER_CO = re.compile(r"^aider\s*\((.*)\)$", re.I)
KEPT_KEYS = {"co-authored-by", "claude-session", "agent-logs-url", "made-with", "assisted-by"}


def review_bot_key(email: str | None) -> str | None:
    """Key of the AI review bot that owns this email, or None."""
    email = (email or "").strip()
    local, _, domain = email.lower().partition("@")
    if domain not in REVIEW_EMAIL_DOMAINS:
        return None
    if COPILOT_REVIEW_EMAIL.match(email):
        return "review:copilot-review"
    local = re.sub(r"^\d+\+", "", local)
    local = re.sub(r"\[bot\]$", "", local)
    return REVIEW_BOT_LOGINS.get(local)


def model_provider(model: str | None) -> str | None:
    if not model:
        return None
    if "/" in model:
        prefix = model.split("/", 1)[0].lower()
        return PREFIX_PROVIDERS.get(prefix, prefix)
    for rx, prov in NAME_PROVIDERS:
        if rx.search(model):
            return prov
    return None


@dataclass
class Use:
    """One AI tool seen in one commit."""
    key: str
    name: str
    vendor: str | None
    model: str | None
    evidence: str  # "trailer" or "note"
    traces: list = field(default_factory=list)  # the trailer lines that prove it
    kind: str = KIND_CODING

    @property
    def is_review(self) -> bool:
        return self.kind == KIND_REVIEW

    @property
    def model_provider(self) -> str | None:
        return model_provider(self.model)

    def providers(self) -> list:
        out = []
        for p in (self.vendor, self.model_provider):
            if p and p not in out:
                out.append(p)
        return out

    def ident(self) -> tuple:
        return (self.name, self.model or "", self.vendor or "", self.model_provider or "",
                self.kind)


def make_use(key, evidence, model=None, trace=None, name=None):
    kind = KIND_CODING
    if key in REVIEW_BOTS:
        disp, vendor = REVIEW_BOTS[key]
        kind = KIND_REVIEW
    else:
        disp, vendor = TOOLS.get(key, (name or key, None))
    return Use(key=key, name=disp, vendor=vendor, model=model, evidence=evidence,
               traces=[trace] if trace else [], kind=kind)


@dataclass
class FileStat:
    path: str
    added: int
    deleted: int
    binary: bool = False


@dataclass
class Commit:
    sha: str
    date: str
    uses: list
    files: list  # FileStat
    ai_lines: dict  # path -> int (AI-attributed added lines in this commit)
    basis: str  # "note", "trailer" or "none" (about coding tools only)
    review_lines: dict = field(default_factory=dict)  # path -> lines added, when a review bot co-authored


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------
class GitError(Exception):
    pass


def git(repo, *args, check=True) -> bytes:
    cmd = ["git", "-C", repo, "-c", "core.quotepath=off", *args]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout


def git_text(repo, *args, check=True) -> str:
    return git(repo, *args, check=check).decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Trailer detection
# --------------------------------------------------------------------------
def kept_trailers(message: str) -> list:
    """Trailer-shaped lines with a known key, from anywhere in the message.

    Anywhere, not only the last block, because squash merges keep each
    original commit's trailers inside the body.
    """
    out = []
    for line in message.splitlines():
        m = TRAILER_LINE.match(line)
        if m and m.group(1).lower() in KEPT_KEYS:
            out.append((m.group(1), m.group(2)))
    return out


def detect_trailers(author_name, author_email, committer_name, subject, trailers) -> list:
    uses: dict = {}

    def add(key, trace, model=None, name=None):
        u = uses.get(key)
        if u is None:
            u = make_use(key, "trailer", model=model, trace=None, name=name)
            uses[key] = u
        if model and not u.model:
            u.model = model
        if trace and trace not in u.traces:
            u.traces.append(trace)

    is_copilot_author = bool(COPILOT_EMAIL.match(author_email or ""))
    is_claude_gh_author = bool(CLAUDE_GH_EMAIL.match(author_email or ""))
    if COPILOT_REVIEW_EMAIL.match(author_email or ""):
        add("review:copilot-review", f"Author: {author_name} <{author_email}>")
    if is_copilot_author:
        add("copilot-agent", f"Author: {author_name} <{author_email}>")
    if is_claude_gh_author:
        add("claude-github", f"Author: {author_name} <{author_email}>")
    if (author_name or "").endswith(" (aider)"):
        add("aider", "Author name ends with (aider)")
    # Committer-only "(aider)" means aider committed human changes: not AI.
    if (subject or "").startswith("aider: "):
        add("aider", "Subject starts with 'aider: '")

    agent_logs = []
    copilot_co = False
    for key, value in trailers:
        k = key.lower()
        line = f"{key}: {value}"
        if k == "claude-session":
            if "claude-github" in uses:
                add("claude-github", line)
            else:
                add("claude-code", line)
        elif k == "agent-logs-url":
            agent_logs.append(line)
        elif k == "made-with":
            if value.strip().lower() == "cursor":
                add("cursor", line)
        elif k == "assisted-by":
            # AGENT:MODEL [TOOLS]
            first = value.split()[0] if value.split() else value
            agent, _, model = first.partition(":")
            if agent:
                add("assisted:" + agent.lower(), line, model=model or None, name=agent)
        elif k == "co-authored-by":
            m = NAME_EMAIL.match(value)
            if not m:
                continue
            name, email = m.group(1).strip(), m.group(2).strip().lower()
            review_key = review_bot_key(email)
            if review_key:
                add(review_key, line)
            elif email == "noreply@anthropic.com":
                model = None
                if name.lower().startswith("claude") and name.lower() not in ("claude", "claude code"):
                    model = name
                elif name.lower() == "claude":
                    model = "Claude (version not stated)"
                target = "claude-github" if "claude-github" in uses else "claude-code"
                add(target, line, model=model)
            elif COPILOT_EMAIL.match(email):
                copilot_co = True
                add("copilot-agent", line)
            elif email == "cursoragent@cursor.com":
                add("cursor", line)
            elif email == "noreply@openai.com" and name.lower() == "codex":
                add("codex", line)
            elif email == "aider@aider.chat":
                am = AIDER_CO.match(name)
                model = am.group(1).strip() if am else None
                if model == "unknown-model":
                    model = None
                add("aider", line, model=model)
            # Human co-authors are ignored and never output.

    for line in agent_logs:
        if is_copilot_author or copilot_co:
            add("copilot-agent", line)
        elif is_claude_gh_author:
            add("claude-github", line)
        else:
            add("github-agent", line)
    # If the unnamed GitHub agent and a named one are both present, the named
    # one explains the trailer.
    if "github-agent" in uses and ({"copilot-agent", "claude-github"} & set(uses)):
        named = "copilot-agent" if "copilot-agent" in uses else "claude-github"
        for t in uses.pop("github-agent").traces:
            add(named, t)
    return list(uses.values())


# --------------------------------------------------------------------------
# git-ai notes
# --------------------------------------------------------------------------
def parse_ranges(text: str) -> int:
    n = 0
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if b >= a:
                n += b - a + 1
        else:
            int(part)
            n += 1
    return n


def unquote_path(p: str) -> str:
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            return json.loads(p)
        except ValueError:
            return p[1:-1]
    return p


def parse_note(text: str):
    """Return (uses, ai_lines_per_file) from one git-ai note, or None if invalid."""
    lines = text.splitlines()
    try:
        sep = lines.index("---")
    except ValueError:
        return None
    try:
        meta = json.loads("\n".join(lines[sep + 1:]))
    except ValueError:
        return None
    if not isinstance(meta, dict):
        return None
    prompts = meta.get("prompts") or {}
    sessions = meta.get("sessions") or {}

    def agent_for(key):
        if key.startswith("h_"):
            return None  # a known human
        if "::" in key:
            rec = sessions.get(key.split("::", 1)[0])
        else:
            rec = prompts.get(key)
        if not isinstance(rec, dict):
            return {"tool": "unknown", "model": None}
        aid = rec.get("agent_id") or {}
        return {"tool": str(aid.get("tool") or "unknown"), "model": aid.get("model") or None}

    uses: dict = {}
    per_file: dict = {}
    current = None
    for line in lines[:sep]:
        if not line.strip():
            continue
        if not line.startswith(" "):
            current = unquote_path(line.rstrip())
            continue
        if current is None or not line.startswith("  "):
            continue
        body = line[2:].strip()
        if " " not in body:
            continue
        key, ranges = body.split(" ", 1)
        agent = agent_for(key)
        if agent is None:
            continue
        try:
            count = parse_ranges(ranges)
        except ValueError:
            continue
        per_file[current] = per_file.get(current, 0) + count
        tkey = GITAI_TOOL_MAP.get(agent["tool"].lower(), "gitai:" + agent["tool"])
        ukey = (tkey, agent["model"])
        if ukey not in uses:
            uses[ukey] = make_use(tkey, "note", model=agent["model"],
                                  trace=f"refs/notes/ai: tool={agent['tool']}"
                                        + (f" model={agent['model']}" if agent["model"] else ""),
                                  name=agent["tool"])
    return list(uses.values()), per_file


def load_notes(repo) -> dict:
    """commit sha -> note blob sha, for refs/notes/ai."""
    out = git_text(repo, "notes", "--ref=ai", "list", check=False)
    notes = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            notes[parts[1]] = parts[0]
    return notes


# --------------------------------------------------------------------------
# Walk the range
# --------------------------------------------------------------------------
def parse_numstat(raw: bytes) -> dict:
    """Parse `git log -M --numstat -z --format=%x1e%H` into sha -> [FileStat]."""
    result = {}
    for chunk in raw.split(b"\x1e"):
        if not chunk.strip(b"\n\x00"):
            continue
        sha_b, _, rest = chunk.partition(b"\x00")
        sha = sha_b.decode().strip()
        toks = rest.lstrip(b"\n").split(b"\x00")
        files = []
        i = 0
        while i < len(toks):
            t = toks[i].lstrip(b"\n")
            i += 1
            if not t:
                continue
            parts = t.decode("utf-8", "replace").split("\t", 2)
            if len(parts) < 3:
                continue
            a, d, path = parts
            if path == "":
                # rename or copy: the next two tokens are the old and new path
                path = toks[i + 1].decode("utf-8", "replace") if i + 1 < len(toks) else ""
                i += 2
            binary = a == "-"
            files.append(FileStat(path=path, added=0 if binary else int(a),
                                  deleted=0 if binary else int(d), binary=binary))
        result[sha] = files
    return result


def walk(repo, since, until) -> list:
    rng = f"{since}..{until}" if since else until
    fmt = "%x1e%H%x1f%cI%x1f%an%x1f%ae%x1f%cn%x1f%B"
    raw = git_text(repo, "log", "--no-merges", f"--format={fmt}", rng)
    numstat = parse_numstat(git(repo, "log", "--no-merges", "-M", "--numstat", "-z",
                                "--format=%x1e%H", rng))
    notes = load_notes(repo)
    commits = []
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        parts = rec.split("\x1f", 5)
        if len(parts) < 6:
            continue
        sha, date, an, ae, cn, msg = (p.strip() if i < 5 else p for i, p in enumerate(parts))
        subject = msg.strip().splitlines()[0] if msg.strip() else ""
        uses = detect_trailers(an, ae, cn, subject, kept_trailers(msg))
        del msg, subject  # nothing else from the message is kept
        files = numstat.get(sha, [])
        ai_lines = {}
        basis = "none"
        if sha in notes:
            parsed = parse_note(git_text(repo, "cat-file", "-p", notes[sha], check=False))
            if parsed is not None:
                note_uses, per_file = parsed
                if note_uses:
                    known = {(u.key, u.model) for u in uses}
                    for u in note_uses:
                        if (u.key, u.model) not in known:
                            uses.append(u)
                    added = {f.path: f.added for f in files}
                    for path, n in per_file.items():
                        ai_lines[path] = min(n, added.get(path, n))
                    basis = "note"
        # Review bots are counted apart from coding tools: they only change basis
        # and ai_lines if a coding tool is also there.
        if basis == "none" and any(not u.is_review for u in uses):
            basis = "trailer"
            for f in files:
                ai_lines[f.path] = f.added
        review_lines = {f.path: f.added for f in files} if any(u.is_review for u in uses) else {}
        commits.append(Commit(sha=sha, date=date, uses=uses, files=files,
                              ai_lines=ai_lines, basis=basis, review_lines=review_lines))
    commits.reverse()  # oldest first
    return commits


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------
def aggregate(commits):
    files: dict = {}
    tools: dict = {}
    for c in commits:
        tool_names = sorted({u.name for u in c.uses if not u.is_review})
        review_names = sorted({u.name for u in c.uses if u.is_review})
        for u in c.uses:
            t = tools.setdefault(u.ident(), {"use": u, "commits": [], "evidence": set()})
            t["commits"].append(c.sha)
            t["evidence"].add(u.evidence)
        for f in c.files:
            e = files.setdefault(f.path, {"path": f.path, "added": 0, "deleted": 0,
                                          "ai_lines": 0, "basis": set(), "tools": set(),
                                          "ai_commits": [], "binary": False,
                                          "review_lines": 0, "review_tools": set(),
                                          "review_commits": []})
            e["added"] += f.added
            e["deleted"] += f.deleted
            e["binary"] = e["binary"] or f.binary
            if c.basis != "none" and (c.basis == "trailer" or f.path in c.ai_lines):
                n = c.ai_lines.get(f.path, 0)
                e["ai_lines"] += n
                e["basis"].add(c.basis)
                e["tools"].update(tool_names)
                e["ai_commits"].append(c.sha)
            if review_names and f.path in c.review_lines:
                e["review_lines"] += c.review_lines[f.path]
                e["review_tools"].update(review_names)
                e["review_commits"].append(c.sha)
    return files, tools


def file_digest(repo, until, path):
    p = subprocess.run(["git", "-C", repo, "cat-file", "blob", f"{until}:{path}"],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if p.returncode != 0:
        return None
    return hashlib.sha256(p.stdout).hexdigest()


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------
def load_policy(path):
    with open(path, encoding="utf-8") as fh:
        pol = json.load(fh)
    if not isinstance(pol, dict):
        raise ValueError("policy must be a JSON object")
    return pol


def check_policy(policy, commits) -> list:
    tools_ok = {t.lower() for t in policy.get("allowed_tools", [])}
    provs = policy.get("allowed_providers")
    provs_ok = {p.lower() for p in provs} if provs is not None else None
    allow_unknown = bool(policy.get("allow_unknown_provider", False))
    problems = []
    for c in commits:
        for u in c.uses:
            reasons = []
            if u.name.lower() not in tools_ok and u.key.lower() not in tools_ok:
                reasons.append(f"tool '{u.name}' is not on the allowed list")
            if provs_ok is not None:
                ps = u.providers()
                if not ps:
                    if not allow_unknown:
                        reasons.append("provider unknown")
                for p in ps:
                    if p.lower() not in provs_ok:
                        reasons.append(f"provider '{p}' is not on the allowed list")
            if reasons:
                problems.append({"commit": c.sha, "tool": u.name, "model": u.model,
                                 "kind": u.kind, "reasons": reasons})
    return problems


# --------------------------------------------------------------------------
# Output builders
# --------------------------------------------------------------------------
def now_utc():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "x"


def tool_label(u):
    return u.name + (f" ({u.model})" if u.model else "")


def release_info(repo, since, until):
    top = git_text(repo, "rev-parse", "--show-toplevel").strip()
    name = os.path.basename(top.rstrip("/\\")) or "release"
    version = git_text(repo, "describe", "--tags", "--always", until, check=False).strip() or until
    head = git_text(repo, "rev-parse", until).strip()
    return {"name": name, "version": version, "commit": head, "since": since or "(root)",
            "until": until}


def build_cyclonedx(rel, commits, files, tools, repo, until):
    rel_ref = "release"
    services = []
    models = []
    seen_models = set()
    tool_ref = {}
    for i, (ident, t) in enumerate(sorted(tools.items(), key=lambda kv: kv[0])):
        u = t["use"]
        ref = f"ai-tool-{i + 1}-{slug(u.name)}"
        tool_ref[ident] = ref
        svc = {"bom-ref": ref, "name": u.name}
        if u.vendor:
            svc["provider"] = {"name": u.vendor}
        props = [{"name": "aiprov:kind", "value": u.kind},
                 {"name": "aiprov:model", "value": u.model or "not recorded"},
                 {"name": "aiprov:model.provider", "value": u.model_provider or "not recorded"},
                 {"name": "aiprov:evidence", "value": ",".join(sorted(t["evidence"]))},
                 {"name": "aiprov:commit.count", "value": str(len(t["commits"]))}]
        svc["properties"] = props
        services.append(svc)
        if u.model and u.model not in seen_models:
            seen_models.add(u.model)
            mc = {"type": "machine-learning-model", "bom-ref": f"ai-model-{slug(u.model)}",
                  "name": u.model}
            if u.model_provider:
                mc["supplier"] = {"name": u.model_provider}
            models.append(mc)

    comps = []
    for path in sorted(files):
        e = files[path]
        c = {"type": "file", "bom-ref": f"file:{path}", "name": path}
        digest = file_digest(repo, until, path)
        if digest:
            c["hashes"] = [{"alg": "SHA-256", "content": digest}]
        props = [{"name": "aiprov:lines.added", "value": str(e["added"])}]
        if digest is None:
            props.append({"name": "aiprov:file.deleted", "value": "true"})
        if e["ai_commits"]:
            props.insert(0, {"name": "AI Generated", "value": "true"})
            props += [
                {"name": "aiprov:ai.lines", "value": str(e["ai_lines"])},
                {"name": "aiprov:ai.lines.basis", "value": ",".join(sorted(e["basis"]))},
                {"name": "aiprov:ai.tools", "value": "; ".join(sorted(e["tools"]))},
                {"name": "aiprov:ai.commits", "value": ",".join(s[:12] for s in e["ai_commits"])},
            ]
        if e["review_commits"]:
            # Kept apart from the AI Generated flag: a bot wrote suggested lines inside
            # these commits, not the commits as a whole.
            props += [
                {"name": "aiprov:review.lines", "value": str(e["review_lines"])},
                {"name": "aiprov:review.bots", "value": "; ".join(sorted(e["review_tools"]))},
                {"name": "aiprov:review.commits",
                 "value": ",".join(s[:12] for s in e["review_commits"])},
            ]
        c["properties"] = props
        comps.append(c)

    commit_props = []
    for c in commits:
        if not c.uses:
            continue
        for u in c.uses:
            commit_props.append({"name": "aiprov:review.commit" if u.is_review else "aiprov:commit",
                                 "value": f"{c.sha} | {tool_label(u)} | {u.evidence} | "
                                          + " ; ".join(u.traces)})

    ai_files = sum(1 for e in files.values() if e["ai_commits"])
    review_files = sum(1 for e in files.values() if e["review_commits"])
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": now_utc(),
            "tools": {"components": [{"type": "application", "name": "aiprov",
                                      "version": VERSION}]},
            "component": {
                "type": "application", "bom-ref": rel_ref, "name": rel["name"],
                "version": rel["version"],
                "properties": [
                    {"name": "aiprov:range", "value": f"{rel['since']}..{rel['until']}"},
                    {"name": "aiprov:release.commit", "value": rel["commit"]},
                    {"name": "aiprov:commits.total", "value": str(len(commits))},
                    {"name": "aiprov:commits.ai",
                     "value": str(sum(1 for c in commits if any(not u.is_review for u in c.uses)))},
                    {"name": "aiprov:commits.review",
                     "value": str(sum(1 for c in commits if any(u.is_review for u in c.uses)))},
                    {"name": "aiprov:files.ai", "value": str(ai_files)},
                    {"name": "aiprov:files.review", "value": str(review_files)},
                    {"name": "AI Generated",
                     "value": "true" if ai_files else "false"},
                ],
            },
        },
        "components": comps,
        "formulation": [{
            "bom-ref": "ai-formulation",
            "components": models,
            "services": services,
            "properties": commit_props,
        }],
    }
    return bom


def build_spdx(rel, commits, files, tools, repo, until):
    ns = f"urn:aiprov:{slug(rel['name'])}:{slug(rel['version'])}:{uuid.uuid4()}"
    ci = "_:creationinfo"
    graph = []
    agent_id = f"{ns}#aiprov-agent"
    tool_id = f"{ns}#aiprov-tool"
    graph.append({"type": "CreationInfo", "@id": ci, "specVersion": "3.0.1",
                  "created": now_utc(), "createdBy": [agent_id], "createdUsing": [tool_id]})
    graph.append({"type": "SoftwareAgent", "spdxId": agent_id, "creationInfo": ci,
                  "name": "aiprov"})
    graph.append({"type": "Tool", "spdxId": tool_id, "creationInfo": ci,
                  "name": f"aiprov {VERSION}"})
    pkg_id = f"{ns}#release"
    graph.append({"type": "software_Package", "spdxId": pkg_id, "creationInfo": ci,
                  "name": rel["name"], "software_packageVersion": rel["version"],
                  "comment": f"git range {rel['since']}..{rel['until']} at {rel['commit']}"})
    elements = [agent_id, tool_id, pkg_id]

    file_ids = {}
    rel_n = [0]

    def relid():
        rel_n[0] += 1
        return f"{ns}#rel-{rel_n[0]}"

    for path in sorted(files):
        e = files[path]
        fid = f"{ns}#file-{hashlib.sha1(path.encode()).hexdigest()[:16]}"
        file_ids[path] = fid
        el = {"type": "software_File", "spdxId": fid, "creationInfo": ci, "name": path}
        digest = file_digest(repo, until, path)
        if digest:
            el["verifiedUsing"] = [{"type": "Hash", "algorithm": "sha256", "hashValue": digest}]
        else:
            el["comment"] = "deleted by the end of the range"
        graph.append(el)
        elements.append(fid)
    if file_ids:
        rid = relid()
        graph.append({"type": "Relationship", "spdxId": rid, "creationInfo": ci,
                      "from": pkg_id, "relationshipType": "contains",
                      "to": [file_ids[p] for p in sorted(file_ids)]})
        elements.append(rid)

    orgs = {}
    for i, (ident, t) in enumerate(sorted(tools.items(), key=lambda kv: kv[0])):
        u = t["use"]
        tid = f"{ns}#ai-tool-{i + 1}"
        summary = (f"{u.kind}. model: {u.model or 'not recorded'}; "
                   f"tool vendor: {u.vendor or 'not recorded'}; "
                   f"model provider: {u.model_provider or 'not recorded'}; "
                   f"evidence: {','.join(sorted(t['evidence']))}")
        graph.append({"type": "Tool", "spdxId": tid, "creationInfo": ci,
                      "name": tool_label(u), "summary": summary,
                      "comment": f"aiprov:kind={u.kind}"})
        elements.append(tid)
        # release usesTool AI tool
        rid = relid()
        graph.append({"type": "Relationship", "spdxId": rid, "creationInfo": ci,
                      "from": pkg_id, "relationshipType": "usesTool", "to": [tid]})
        elements.append(rid)
        # AI tool generates files (files touched by commits that used it)
        # A review bot did not write whole files, so it gets no "generates" link.
        # Its commits are listed in the review annotations below.
        touched = [] if u.is_review else sorted(
            {f.path for c in commits for f in c.files
             if c.sha in t["commits"] and f.path in file_ids
             and (c.basis == "trailer" or f.path in c.ai_lines)})
        if touched:
            rid = relid()
            graph.append({"type": "Relationship", "spdxId": rid, "creationInfo": ci,
                          "from": tid, "relationshipType": "generates",
                          "to": [file_ids[p] for p in touched],
                          "completeness": "noAssertion",
                          "comment": "The tool wrote some or all of the added lines in these "
                                     "files. See the AI Generated annotations for line counts."})
            elements.append(rid)
        for p in u.providers():
            if p not in orgs:
                oid = f"{ns}#org-{slug(p)}"
                orgs[p] = oid
                graph.append({"type": "Organization", "spdxId": oid, "creationInfo": ci,
                              "name": p})
                elements.append(oid)
            rid = relid()
            graph.append({"type": "Relationship", "spdxId": rid, "creationInfo": ci,
                          "from": tid, "relationshipType": "availableFrom",
                          "to": [orgs[p]]})
            elements.append(rid)

    for path in sorted(files):
        e = files[path]
        if not e["ai_commits"]:
            continue
        aid = f"{ns}#ann-{hashlib.sha1(path.encode()).hexdigest()[:16]}"
        graph.append({"type": "Annotation", "spdxId": aid, "creationInfo": ci,
                      "annotationType": "other", "subject": file_ids[path],
                      "statement": (f"AI Generated. ai_lines={e['ai_lines']} of "
                                    f"lines_added={e['added']}; basis="
                                    f"{','.join(sorted(e['basis']))}; tools="
                                    f"{'; '.join(sorted(e['tools']))}; commits="
                                    f"{','.join(s[:12] for s in e['ai_commits'])}")})
        elements.append(aid)

    for path in sorted(files):
        e = files[path]
        if not e["review_commits"]:
            continue
        aid = f"{ns}#rev-{hashlib.sha1(path.encode()).hexdigest()[:16]}"
        graph.append({"type": "Annotation", "spdxId": aid, "creationInfo": ci,
                      "annotationType": "other", "subject": file_ids[path],
                      "statement": (f"AI review bot suggestion (not AI Generated). "
                                    f"lines_in_commits={e['review_lines']} of "
                                    f"lines_added={e['added']}; bots="
                                    f"{'; '.join(sorted(e['review_tools']))}; commits="
                                    f"{','.join(s[:12] for s in e['review_commits'])}")})
        elements.append(aid)

    doc_id = f"{ns}#document"
    graph.append({"type": "SpdxDocument", "spdxId": doc_id, "creationInfo": ci,
                  "name": f"AI provenance for {rel['name']} {rel['version']}",
                  "dataLicense": "https://spdx.org/licenses/CC0-1.0",
                  "profileConformance": ["core", "software"],
                  "rootElement": [pkg_id], "element": elements})
    return {"@context": "https://spdx.org/rdf/3.0.1/spdx-context.jsonld", "@graph": graph}


def build_markdown(rel, commits, files, tools, problems, policy_used):
    ai_commits = [c for c in commits if any(not u.is_review for u in c.uses)]
    review_commits = [c for c in commits if any(u.is_review for u in c.uses)]
    ai_files = [e for e in files.values() if e["ai_commits"]]
    review_files = [e for e in files.values() if e["review_commits"]]
    coding_tools = {k: t for k, t in tools.items() if not t["use"].is_review}
    review_tools = {k: t for k, t in tools.items() if t["use"].is_review}
    total_added = sum(e["added"] for e in files.values())
    ai_added = sum(e["ai_lines"] for e in ai_files)
    out = []
    w = out.append
    w(f"# AI use in {rel['name']} {rel['version']}")
    w("")
    w(f"- Range: `{rel['since']}..{rel['until']}` (ends at `{rel['commit'][:12]}`)")
    w(f"- Commits: {len(commits)} (merge commits skipped). With an AI coding tool trace: {len(ai_commits)}.")
    w(f"- With an AI review bot as co-author (counted apart, see below): {len(review_commits)}.")
    w(f"- Files changed: {len(files)}. Touched by AI commits: {len(ai_files)}.")
    w(f"- Lines added: {total_added}. Counted as AI: {ai_added}.")
    w(f"- Made by aiprov {VERSION} on {now_utc()}.")
    w("")
    w("## AI tools seen")
    w("")
    if coding_tools:
        w("| Tool | Model | Tool vendor | Model provider | Commits | Evidence |")
        w("|---|---|---|---|---|---|")
        for ident, t in sorted(coding_tools.items(), key=lambda kv: kv[0]):
            u = t["use"]
            w(f"| {u.name} | {u.model or 'not recorded'} | {u.vendor or '-'} | "
              f"{u.model_provider or 'not recorded'} | {len(t['commits'])} | "
              f"{', '.join(sorted(t['evidence']))} |")
    else:
        w("No AI coding tool traces found in this range.")
    w("")
    w("## Files touched by AI commits")
    w("")
    if ai_files:
        w("| File | Lines added | AI lines | Based on | Tools |")
        w("|---|---|---|---|---|")
        for e in sorted(ai_files, key=lambda e: e["path"]):
            w(f"| `{e['path']}` | {e['added']} | {e['ai_lines']} | "
              f"{', '.join(sorted(e['basis']))} | {'; '.join(sorted(e['tools']))} |")
    else:
        w("None.")
    w("")
    w("## AI commits")
    w("")
    if ai_commits:
        w("| Commit | Date | Tool | Evidence |")
        w("|---|---|---|---|")
        for c in ai_commits:
            for u in c.uses:
                if u.is_review:
                    continue
                tr = "<br>".join(t.replace("|", "\\|") for t in u.traces)
                w(f"| `{c.sha[:12]}` | {c.date[:10]} | {tool_label(u)} | {u.evidence}: {tr} |")
    else:
        w("None.")
    w("")
    w("## AI review bots (applied suggestions)")
    w("")
    w("A review bot posts a suggested change on a pull request. A person then applies it, and "
      "GitHub adds the bot as a co-author. The bot wrote the suggested lines, not the whole "
      "commit. These are not counted in \"Counted as AI\" above.")
    w("")
    if review_tools:
        w("| Bot | Vendor | Commits | Evidence |")
        w("|---|---|---|---|")
        for ident, t in sorted(review_tools.items(), key=lambda kv: kv[0]):
            u = t["use"]
            w(f"| {u.name} | {u.vendor or '-'} | {len(t['commits'])} | "
              f"{', '.join(sorted(t['evidence']))} |")
        w("")
        w("| Commit | Date | Bot | Evidence |")
        w("|---|---|---|---|")
        for c in review_commits:
            for u in c.uses:
                if u.is_review:
                    tr = "<br>".join(t.replace("|", "\\|") for t in u.traces)
                    w(f"| `{c.sha[:12]}` | {c.date[:10]} | {u.name} | {u.evidence}: {tr} |")
        w("")
        w("| File | Lines added | Lines in those commits | Bots |")
        w("|---|---|---|---|")
        for e in sorted(review_files, key=lambda e: e["path"]):
            w(f"| `{e['path']}` | {e['added']} | {e['review_lines']} | "
              f"{'; '.join(sorted(e['review_tools']))} |")
    else:
        w("None.")
    w("")
    w("## Policy check")
    w("")
    if not policy_used:
        w("No policy file given.")
    elif not problems:
        w("Pass. All AI use is on the allowed list.")
    else:
        w(f"**Fail.** {len(problems)} AI use(s) are not on the allowed list:")
        w("")
        for p in problems:
            w(f"- `{p['commit'][:12]}` {p['tool']}"
              + (f" ({p['model']})" if p["model"] else "")
              + (f" [{p['kind']}]" if p["kind"] == KIND_REVIEW else "")
              + ": " + "; ".join(p["reasons"]))
    w("")
    w("## How to read this")
    w("")
    w("- **note**: a git-ai note (refs/notes/ai) lists the exact lines the AI wrote.")
    w("- **trailer**: a commit stamp names the AI tool, but not which lines. All lines added "
      "in that commit are counted as AI, so this is an upper bound.")
    w("- **review bot**: a commit that lists an AI review bot as co-author contains lines the bot "
      "suggested. The other lines in that commit may be human. Its lines are listed apart and "
      "never added to the AI line count.")
    w("- Line counts are lines added across commits, not lines left at the end of the range.")
    w("- This report only sees AI use that left a trace. It does not guess from code style.")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="aiprov",
        description="List AI coding tools that left a trace in a git range, as an SBOM.")
    ap.add_argument("--since", required=True,
                    help="tag or commit the release starts after (excluded). Use ROOT for all history.")
    ap.add_argument("--until", default="HEAD", help="tag or commit the release ends at (default HEAD)")
    ap.add_argument("--policy", help="policy.json with allowed tools and providers")
    ap.add_argument("--format", choices=["cyclonedx", "spdx", "md"], default="cyclonedx")
    ap.add_argument("--repo", default=".", help="path to the git repository (default .)")
    ap.add_argument("--out", help="write output here instead of standard output")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    since = None if args.since.upper() == "ROOT" else args.since
    try:
        policy = load_policy(args.policy) if args.policy else None
    except (OSError, ValueError) as exc:
        print(f"aiprov: cannot read policy: {exc}", file=sys.stderr)
        return 2
    try:
        rel = release_info(args.repo, since, args.until)
        commits = walk(args.repo, since, args.until)
    except GitError as exc:
        print(f"aiprov: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("aiprov: git was not found on PATH", file=sys.stderr)
        return 2

    files, tools = aggregate(commits)
    problems = check_policy(policy, commits) if policy is not None else []

    if args.format == "cyclonedx":
        text = json.dumps(build_cyclonedx(rel, commits, files, tools, args.repo, args.until), indent=2)
    elif args.format == "spdx":
        text = json.dumps(build_spdx(rel, commits, files, tools, args.repo, args.until), indent=2)
    else:
        text = build_markdown(rel, commits, files, tools, problems, policy is not None)

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")

    if problems:
        print(f"aiprov: policy check FAILED: {len(problems)} AI use(s) not allowed", file=sys.stderr)
        for p in problems:
            print(f"  {p['commit']}  {p['tool']}" + (f" ({p['model']})" if p["model"] else "")
                  + (f" [{p['kind']}]" if p["kind"] == KIND_REVIEW else "")
                  + ": " + "; ".join(p["reasons"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
