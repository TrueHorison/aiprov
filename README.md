# aiprov

aiprov reads a git history and lists the AI coding tools that left a trace in it.
It writes the result as a software bill of materials (SBOM) for one release.
It can also check that only approved AI tools and providers were used.

One Python file, standard library only.

## Why

Some large buyers now put AI rules into their supplier terms.
Take AT&T's Supplier AI Requirements (v2.1, October 2025).
Code made with AI must come with a
"software bill of materials (SBOM) including specification of the AI Solution"
in each release.
The same terms ask that AI work is labelled "AI Generated", and that AI tools are approved first.
If a supplier uses AI that was not approved, AT&T may not have to pay.

A supplier then has to answer three questions per release:

1. Which files did AI help write?
2. Which AI tool, model and provider was it?
3. Was each tool on the buyer's approved list?

Coding tools already stamp their commits (see `FORMATS.md`).
aiprov collects those stamps for a release and writes the answers in the two standard SBOM formats.

If a client of yours asks for this, I'd like to know what they ask for and how you deal with it now. There's a [one-minute form](https://docs.google.com/forms/d/e/1FAIpQLSfHLjfFnQ249a2OB8p0qFRYL87-3nQegEcTcPnOnQ6dJpoPrA/viewform), no sign-in needed.

## What it does

For each commit between two refs (merge commits are skipped):

- It reads the commit's AI stamps: `Claude-Session`, `Agent-Logs-Url`, `Made-with`, `Assisted-by`,
  and `Co-authored-by` lines from known AI tools. It also reads the commit author
  (for example `Copilot <198982749+Copilot@users.noreply.github.com>`).
- If the commit has a git-ai note (`refs/notes/ai`), it reads the exact AI lines from the note.
- It also reads `Co-authored-by` lines from AI review bots whose suggestions were applied.
  These are kept apart from coding tools in every output, and their lines are never added to the AI line count.
- It counts the lines added per file, and how many of them count as AI.
- It marks each finding with how it knows: `note`, `trailer` or `none`.

It never copies commit message text other than the AI stamp lines it knows, and never copies diffs.
Human co-author names and emails are not copied either.

Tools it knows today:

- Claude Code, and the Claude agent on GitHub
- GitHub Copilot coding agent, and other GitHub cloud agents (unnamed)
- Cursor, OpenAI Codex and Aider
- `Assisted-by` stamps, and any tool named in a git-ai note
- AI review bots (applied suggestions): Gemini Code Assist, CodeRabbit, GitHub Copilot code review,
  Sourcery, Qodo, Greptile, cubic and OpenAI Codex code review

## Install and run

You need Python 3.11+ and git on your PATH. Nothing to install.

```
python aiprov.py --since v1.0 --until v1.1 --format md
python aiprov.py --since v1.0 --format cyclonedx --out release.cdx.json
python aiprov.py --since v1.0 --format spdx --out release.spdx.json
python aiprov.py --since v1.0 --policy policy.json --format md
```

Options:

| Option | Meaning |
|---|---|
| `--since` | Tag or commit the release starts after (not included). `ROOT` means all history. Required |
| `--until` | Tag or commit the release ends at. Default `HEAD` |
| `--format` | `cyclonedx` (default), `spdx` or `md` |
| `--policy` | Policy file (see below). Without it, no check is made |
| `--repo` | Path to the git repo. Default: the current folder |
| `--out` | Write to this file instead of the screen |

Exit codes: `0` all fine, `1` the policy check failed, `2` bad input or a git error.

If you use git-ai, fetch its notes first: `git fetch origin refs/notes/ai:refs/notes/ai`.

Tests: `python -m unittest -v test_aiprov.py`. They build throwaway git repos.
To also check the outputs against the official schemas, install `jsonschema`.
Then set `AIPROV_SCHEMA_DIR` to a folder with the CycloneDX 1.6 and SPDX 3.0.1 JSON schemas.

## Output examples

From a test repo with three commits: one by Claude Code, one by the Copilot agent, one by hand.

Markdown (cut short):

```
# AI use in demo v1.1

- Range: `v1.0..v1.1` (ends at `4240db79561b`)
- Commits: 3 (merge commits skipped). With an AI coding tool trace: 2.
- With an AI review bot as co-author (counted apart, see below): 0.
- Files changed: 3. Touched by AI commits: 2.
- Lines added: 57. Counted as AI: 52.

## AI tools seen

| Tool | Model | Tool vendor | Model provider | Commits | Evidence |
|---|---|---|---|---|---|
| Claude Code | Claude Opus 5.5 | Anthropic | Anthropic | 1 | trailer |
| GitHub Copilot coding agent | not recorded | GitHub | not recorded | 1 | trailer |

## AI review bots (applied suggestions)

None.

## Policy check

Fail. 1 AI use(s) are not on the allowed list:

- `1cf3b223bf7f` GitHub Copilot coding agent: tool 'GitHub Copilot coding agent' is not on the allowed list; provider 'GitHub' is not on the allowed list
```

CycloneDX (one file entry, cut short):

```json
{
  "type": "file",
  "bom-ref": "file:src/billing.py",
  "name": "src/billing.py",
  "hashes": [{ "alg": "SHA-256", "content": "02a068fc..." }],
  "properties": [
    { "name": "AI Generated", "value": "true" },
    { "name": "aiprov:lines.added", "value": "40" },
    { "name": "aiprov:ai.lines", "value": "40" },
    { "name": "aiprov:ai.lines.basis", "value": "trailer" },
    { "name": "aiprov:ai.tools", "value": "Claude Code" },
    { "name": "aiprov:ai.commits", "value": "038207e80301" }
  ]
}
```

And the AI tool, under `formulation`:

```json
"formulation": [{
  "bom-ref": "ai-formulation",
  "components": [{ "type": "machine-learning-model", "name": "Claude Opus 5.5",
                   "supplier": { "name": "Anthropic" } }],
  "services": [{
    "bom-ref": "ai-tool-1-Claude-Code",
    "name": "Claude Code",
    "provider": { "name": "Anthropic" },
    "properties": [
      { "name": "aiprov:kind", "value": "AI coding tool" },
      { "name": "aiprov:model", "value": "Claude Opus 5.5" },
      { "name": "aiprov:model.provider", "value": "Anthropic" },
      { "name": "aiprov:evidence", "value": "trailer" },
      { "name": "aiprov:commit.count", "value": "1" }
    ]
  }],
  "properties": [{ "name": "aiprov:commit", "value": "<sha> | Claude Code (Claude Opus 5.5) | trailer | Claude-Session: https://claude.ai/code/session_..." }]
}]
```

SPDX 3.0.1: the release is a `software_Package` that `contains` each `software_File`.
Each AI tool is a `Tool`. The release `usesTool` each AI tool.
Each AI tool `generates` the files its commits touched.
Each AI tool is `availableFrom` an `Organization` (the provider).
Each AI file gets an `Annotation` whose statement starts with `AI Generated`.
A review bot is a `Tool` too, with `aiprov:kind=AI review bot (applied suggestion)` in its `comment`.
It gets no `generates` link and no `AI Generated` annotation. Its files get an `Annotation`
that starts with `AI review bot suggestion` instead.
In CycloneDX a review bot is a service with `aiprov:kind` set the same way. Its files get
`aiprov:review.lines`, `aiprov:review.bots` and `aiprov:review.commits`, and no `AI Generated` property.

Both JSON outputs passed the official CycloneDX 1.6 and SPDX 3.0.1 JSON schemas in testing.

## Where the AI tools go in CycloneDX, and why

aiprov puts each AI tool in `services` inside `formulation`, not at the top level. Going by the 1.6 schema text:

- `metadata.tools` is for the tools used to make the SBOM file itself. That is aiprov, and it is listed there.
  Putting Claude Code there would say Claude Code wrote the SBOM.
- Top-level `services` is for services the product uses, such as an API it calls.
  Putting Claude Code there would say the release calls Claude when it runs. That is false.
- `formulation` describes how a component "was manufactured". Its `services` are the
  "transient services" used while making it. An AI coding tool is exactly that.
  The model also goes there, as a `machine-learning-model` component.

The literal `AI Generated` property name matches the buyer's label word for word.
All other properties use an `aiprov:` prefix, as the CycloneDX property taxonomy suggests.
Neither CycloneDX nor SPDX has an agreed field for "this code was written by AI tool X" yet.
This mapping is our choice, not a standard.

## Policy file

```json
{
  "allowed_tools": ["Claude Code", "GitHub Copilot coding agent"],
  "allowed_providers": ["Anthropic", "GitHub"],
  "allow_unknown_provider": false
}
```

- `allowed_tools`: tool names as aiprov prints them (case does not matter).
  Every AI use must name a tool on this list. This includes AI review bots, for example `Gemini Code Assist`.
- `allowed_providers`: optional. If given, every known provider of each use must be on it.
  A use has up to two providers. One is the tool vendor, for example GitHub for Copilot.
  The other is the model provider, for example Anthropic for "Claude Opus 5.5".
- `allow_unknown_provider`: optional, default `false`. When no provider is known
  (for example Aider with no model named), the use fails unless this is `true`.

- `ban_all_ai`: optional, default `false`. For clients that ban AI. When `true`, any stamped commit fails,
  whatever the lists say.

If any use fails, aiprov lists the commits on the error stream and exits with `1`.

## Before you push

Some clients want AI tools approved before code reaches them, not a report after.

```
python aiprov.py hook-install --policy policy.json --remote client
python aiprov.py check --policy policy.json --range origin/main..HEAD
```

The first adds a git pre-push hook. It checks the commits you are about to push. If a tool is not allowed, the push is refused and the commits are listed. `--remote` limits this to matching remotes. An old pre-push hook is kept and runs after.

The second does the same check without a hook, for CI.

Every check adds a line to `.git/aiprov-log.jsonl`: time, remote, range, result and commit ids. No commit text.

It only sees stamped commits. `git push --no-verify` skips the hook. For a real gate, run `check` in CI.

## Limits

- It only sees AI use that left a trace. A stamp can be turned off or lost in a rebase.
  Some AI use never adds one, such as inline suggestions in an editor. aiprov sees none of these.
  An empty report means "no traces", not "no AI".
- Detection guesses are left out on purpose. aiprov does not guess from code style.
  Guessing can't name the tool or provider, which is what the buyer asks for.
- A stamp is an upper bound on lines. A trailer says a tool helped with a commit, not which lines.
  All lines added in that commit count as AI. Only a git-ai note gives exact lines.
- Stamps can be wrong. Anyone can type a trailer. Tools have stamped human work by mistake
  (VS Code's Copilot co-author setting did this in 2026). aiprov reports what the history says.
- The provider route isn't visible. "Claude Opus 5.5" names the model maker.
  It does not say if the model ran through Anthropic, Amazon Bedrock or Google Vertex.
  Copilot and Cursor stamps don't name the model at all.
- Line counts are lines added across commits, not the lines left at the end of the release.
- Merge commits are skipped, so code first written in a merge conflict fix is not counted.
- Aider can be set to put an `aider: ` subject prefix on all its commits, even ones with human edits only.
  aiprov counts those as AI, so it may over-count. A committer-only `(aider)` mark is not counted.
- AI review bots (for example `gemini-code-assist[bot]` and `coderabbitai[bot]`) appear as co-authors when
  someone applies their suggestion. aiprov lists them in their own section. It never counts their lines as AI.
  The bot wrote only the suggested lines, and the rest of the commit may be human.
  It reads a fixed list of bots by their email, so a bot not on the list is not seen.
- Codex and Aider stamps were checked in their source but not yet seen in a real public commit.
- Formats change. `FORMATS.md` lists each one with its source and the date it was checked.
