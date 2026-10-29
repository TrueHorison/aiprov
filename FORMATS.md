# Commit trace formats that aiprov reads (checked 2026-10-28)

Each format below was checked against a primary source: the tool's own docs, its source code, or real public commits.
"Real commit" means I read the commit through the GitHub API. No downloaded code was run.

Status key: **Confirmed** = exact text seen in docs or source and in real commits. **Partly confirmed** = see the note.

## 1. Claude Code (Anthropic)

| Trace | Exact form | Status |
|---|---|---|
| Co-author trailer | `Co-Authored-By: <name> <noreply@anthropic.com>` | Confirmed |
| Session trailer | `Claude-Session: https://claude.ai/code/session_<id>` | Confirmed (form from real commits; the docs name the trailer but give no example) |

- `<name>` is the model in use, for example `Claude Opus 5.5`. If Claude Code knows it is a Claude model but not the version, it writes `Claude`. If it can't match the model to any Claude model (for example a third-party model behind a custom `ANTHROPIC_BASE_URL`), it writes `Claude Code`.
- Users can change or hide the trailer with the `attribution.commit` setting. An empty string hides it. `includeCoAuthoredBy: false` (deprecated) also hides it.
- The session link is controlled by `attribution.sessionUrl` (default `true`). The settings index describes it as the link "from cloud and Remote Control commits", so local terminal commits may not carry it.
- Sources:
  - Settings reference, `attribution` and `includeCoAuthoredBy` entries: https://code.claude.com/docs/en/settings-reference.md
  - Real commits: https://github.com/JeSuisDodo/projet_jeu_prof/commit/a922d90565 (both trailers), https://github.com/archipel-dev/ide/commit/8f1b61664c

## 2. GitHub Copilot coding agent (cloud agent)

| Trace | Exact form | Status |
|---|---|---|
| Session trailer | `Agent-Logs-Url: https://github.com/<owner>/<repo>/sessions/<uuid>` | Confirmed (form from real commits; the changelog names the trailer but gives no example) |
| Commit author | `Copilot <198982749+Copilot@users.noreply.github.com>` | Confirmed |
| Co-author line (human) | `Co-authored-by: <human> <...>` names the person who gave the task | Confirmed |
| Co-author line after a squash merge | `Co-authored-by: copilot-swe-agent[bot] <198982749+Copilot@users.noreply.github.com>` | Confirmed (real commit) |

- GitHub says the agent's commits are "authored by Copilot", with the human as co-author.
- In a squash merge, the agent's per-commit `Agent-Logs-Url` lines end up in the body of the single squashed commit, not in its last trailer block. aiprov reads trailer-shaped lines anywhere in the message for this reason.
- **The same `Agent-Logs-Url` trailer is used by other agents running on GitHub.** A real commit in `oxc-project/oxc-vscode` has author `Claude <242468646+Claude@users.noreply.github.com>` and an `Agent-Logs-Url` trailer. So the trailer alone says "a GitHub cloud agent", and the commit author says which one.
- Sources:
  - GitHub changelog, 20 Mar 2026: https://github.blog/changelog/2026-03-20-trace-any-copilot-coding-agent-commit-to-its-session-logs
  - Real commits: https://github.com/tjorim/pyrail/commit/b54cf2126b (Copilot, squash merge), https://github.com/oxc-project/oxc-vscode/commit/42a800afbb (Claude agent on GitHub)

## 3. Cursor (Anysphere)

| Trace | Exact form | Status |
|---|---|---|
| Co-author line (cloud agents) | `Co-authored-by: Cursor <cursoragent@cursor.com>` | Partly confirmed: seen in real commits and user forum posts; Cursor's own docs don't print it |
| Made-with trailer (IDE and CLI agents) | `Made-with: Cursor` | Partly confirmed: seen in real commits. The docs call it the "Made with Cursor" trailer and don't print the exact key |

- Attribution is on by default. It is set under Cursor Settings > Agent > Attribution (Git & PRs > Attribution in 3.11+). Team admins can turn it off for everyone.
- Neither trace names the model.
- Sources:
  - Cursor docs: https://cursor.com/docs/integrations/git
  - Forum thread (users quoting the co-author line): https://forum.cursor.com/t/co-author-added-without-consent-and-cant-be-turned-off/150096
  - Real commits: https://github.com/Tencent-Hunyuan/Hy-Memory/commit/6d1d29e16d (co-author line), https://github.com/RevenueCat/purchases-kmp/commit/62df795141 (`Made-with: Cursor`)

## 4. OpenAI Codex

| Trace | Exact form | Status |
|---|---|---|
| Co-author line | `Co-authored-by: Codex <noreply@openai.com>` | Confirmed in source |

- The Codex source tells the agent to end commit messages with this exact trailer, once. The PR text is `Generated with [Codex](https://openai.com/codex/).`
- Whether it is on by default is not settled. Third-party pages disagree on whether a feature flag (`codex_git_commit`) is needed and how `commit_attribution` interacts with it. I could not read the current config reference in full. aiprov only needs the trailer text, which is confirmed.
- Sources (read, not run):
  - https://github.com/openai/codex/blob/main/codex-rs/ext/git-attribution/src/world_state.rs
  - https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/git_attribution.rs
- I did not find a real public commit with this trailer in this pass (GitHub search rate limit).

## 5. Aider (open source)

| Trace | Exact form | Default |
|---|---|---|
| Author and committer name | `<your name> (aider)` | On |
| Co-author line | `Co-authored-by: aider (<model>) <aider@aider.chat>`; `<model>` is `unknown-model` if none is known | Off |
| Message prefix | `aider: ` at the start of the subject line | Off |

- If aider only committed changes the user had already made, it adds `(aider)` to the **committer** name only. aiprov treats a committer-only mark as **not** AI-written.
- `--attribute-commit-message-committer` adds the `aider: ` prefix to **all** aider commits, AI-written or not. aiprov can't tell which option was used, so a prefix counts as AI use. This may over-count.
- Sources:
  - Docs: https://aider.chat/docs/git.html
  - Source (read, not run): https://github.com/Aider-AI/aider/blob/main/aider/repo.py
- Not confirmed in a real public commit in this pass (GitHub search rate limit).

## 6. Linux kernel style `Assisted-by`

- Form: `Assisted-by: AGENT:MODEL [TOOLS]`. The kernel adopted it in Apr 2026 and is retiring it (see `../rivals-ai-code-sbom.md`). aiprov reads it because it names the model.
- Not re-checked at source in this pass.

## 7. git-ai notes (`refs/notes/ai`)

- Ref: `refs/notes/ai`. One note per commit at most. The default `refs/notes/commits` is not used.
- Note layout: an **attestation section**, then a line that is only `---`, then a **JSON metadata section**.
- Attestation section:
  - A file path at column 0 (quoted if it has spaces, tabs or newlines). Files with nothing attested are left out.
  - Under it, lines indented by exactly two spaces: `<key> <ranges>`. Ranges are 1-based, ascending, no spaces, for example `1-12,14`. Lines not listed are untracked.
  - Key forms: `s_<14 hex>::t_<14 hex>` (session, look up the part before `::` in `sessions`), `h_<14 hex>` (a known human, look up in `humans`), or 16 hex chars with no prefix (older notes, look up in `prompts`). Very old notes may use 7-char hashes.
- Metadata JSON:
  - Required: `schema_version` (`authorship/3.0.0`), `base_commit_sha`, `prompts`.
  - Optional: `git_ai_version`, `sessions`, `humans`.
  - `prompts` entries (older): `agent_id` {`tool`, `id`, `model`}, plus line counts `total_additions`, `total_deletions`, `accepted_lines`, `overriden_lines` (spelled that way), and optional `human_author`, `messages_url`, `custom_attributes`.
  - `sessions` entries: `agent_id` {`tool`, `id`, `model`}, optional `human_author`, `custom_attributes`. No prompts or messages.
  - `humans` entries: `author` as `Name <email>`.
- Prompts are kept outside git; notes don't hold prompt text.
- git-ai rewrites notes on rebase, squash and cherry-pick, and syncs them on push and fetch.
- I did not confirm the exact `agent_id.tool` values (for example `claude`, `cursor`, `codex`). aiprov maps the ones it knows and passes others through as written.
- Sources:
  - Spec: https://github.com/git-ai-project/git-ai/blob/main/specs/git_ai_standard_v3.0.0.md
  - Repo: https://github.com/git-ai-project/git-ai

## Standards used for output

- CycloneDX 1.6 JSON schema: https://raw.githubusercontent.com/CycloneDX/specification/1.6/schema/bom-1.6.schema.json
- SPDX 3.0.1 JSON schema: https://spdx.org/schema/3.0.1/spdx-json-schema.json and context https://spdx.org/rdf/3.0.1/spdx-context.jsonld
