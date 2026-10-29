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

## 8. AI review bots that co-author applied suggestions (checked 2026-10-29)

aiprov reports these as kind `AI review bot (applied suggestion)`, apart from coding tools.

How the trailer gets there: a review bot posts a suggested change on a pull request. When a person clicks "Commit suggestion" (or "Add suggestion to batch"), GitHub adds each person or bot that suggested a change as a co-author. GitHub's docs say: "Each person who suggested a change included in the commit will be a co-author of the commit." The human who applies it stays the author. After a squash merge, the trailer lines of all squashed commits end up in the message body, often repeated many times. aiprov reads them anywhere in the message and keeps each once.

| Bot (account) | Exact co-author line seen in real commits | Real commit | Status |
|---|---|---|---|
| Gemini Code Assist (`gemini-code-assist[bot]`, id 176961590) | `Co-authored-by: gemini-code-assist[bot] <176961590+gemini-code-assist[bot]@users.noreply.github.com>` | https://github.com/project-chip/matter-handbook/commit/b0c1fd40c2 and https://github.com/tjorim/pyrail/commit/d62ef5d0 | Confirmed |
| Gemini Code Assist, no id in email | `Co-authored-by: gemini-code-assist[bot] <gemini-code-assist[bot]@users.noreply.github.com>` | https://github.com/juninmd/shorts-generator/commit/3f0cc508a4 | Confirmed |
| Gemini Code Assist, other display name | `Co-authored-by: Antigravity <gemini-code-assist[bot]@users.noreply.github.com>` | https://github.com/rmems/Theseus-Quarry/commit/138dd36ec2 | Confirmed (name differs, same account) |
| CodeRabbit (`coderabbitai[bot]`, id 136622811) | `Co-authored-by: coderabbitai[bot] <136622811+coderabbitai[bot]@users.noreply.github.com>` | https://github.com/Ferrocite/ferrox/commit/e658549bd5, https://github.com/fossasia/eventyay-checkin/commit/18468c7fd0 | Confirmed |
| CodeRabbit EU (`coderabbiteu[bot]`) | `Co-authored-by: coderabbiteu[bot] <274295283+coderabbiteu[bot]@users.noreply.github.com>` | https://github.com/NicolasDiCostanzo/OpenCertCraft/commit/614c492cda | Confirmed (one repo seen) |
| GitHub Copilot code review (`copilot-pull-request-reviewer[bot]`, id 175728472) | `Co-authored-by: copilot-pull-request-reviewer[bot] <175728472+copilot-pull-request-reviewer[bot]@users.noreply.github.com>` | https://github.com/Fueled/oss-metrics/commit/b38d1f49c1 | Confirmed |
| Copilot code review, no id | `Co-authored-by: copilot-pull-request-reviewer <copilot-pull-request-reviewer@users.noreply.github.com>` | https://github.com/microsoft/finops-toolkit/commit/5b6428c9b1 | Confirmed |
| Copilot code review, github.com email | `Co-authored-by: Copilot <copilot-pull-request-reviewer@github.com>` | https://github.com/microbiomedata/nmdc-schema/commit/02245dcf83 | Confirmed |
| Copilot code review, shown as "Copilot" | `Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>` | https://github.com/yutto-dev/yutto/commit/c40272a80c | Confirmed |
| Sourcery (`sourcery-ai[bot]`, id 58596630) | `Co-authored-by: sourcery-ai[bot] <58596630+sourcery-ai[bot]@users.noreply.github.com>` | https://github.com/fossasia/eventyay-checkin/commit/18468c7fd0 | Confirmed |
| Qodo (`qodo-code-review[bot]`) | `Co-authored-by: qodo-code-review[bot] <151058649+qodo-code-review[bot]@users.noreply.github.com>` (also seen without the id) | https://github.com/fleekdash/extensions/commit/734483cc83 | Confirmed |
| Greptile (`greptile-apps[bot]`) | `Co-authored-by: greptile-apps[bot] <165735046+greptile-apps[bot]@users.noreply.github.com>` | https://github.com/eduardofrancener/rustdesk/commit/4515cd6463 | Confirmed |
| cubic (`cubic-dev-ai[bot]`) | `Co-authored-by: cubic-dev-ai[bot] <191113872+cubic-dev-ai[bot]@users.noreply.github.com>` | https://github.com/GLORIFIER7/GLORIFIER/commit/77c8a4707b | Confirmed |
| OpenAI Codex code review (`chatgpt-codex-connector[bot]`) | `Co-authored-by: chatgpt-codex-connector[bot] <199175422+chatgpt-codex-connector[bot]@users.noreply.github.com>` (the display name is sometimes `Codex (<model>)`) | https://github.com/trvny/trvny/commit/34d3010827 | Confirmed |

How I checked: GitHub commit search (read-only) for each bot name, then read the commit message through the GitHub API. For every row above I read the commit and saw the exact line. Account ids come from `GET /users/<login>` (gemini 176961590, coderabbitai 136622811, sourcery-ai 58596630, copilot-pull-request-reviewer[bot] 175728472, copilot-swe-agent[bot] 198982749). Search turned up many thousands of commits for the first four bots.

What aiprov matches: the email only, never the display name. It takes the part before the `@`, drops a leading `<id>+` and a trailing `[bot]`, and looks that up. The domain must be `users.noreply.github.com` or `github.com`. A human who types a bot's name with some other email is not matched.

Traps found:
- **The Copilot reviewer shows up as "Copilot".** `175728472+Copilot@users.noreply.github.com` is the code review bot. The Copilot coding agent is `198982749+Copilot@users.noreply.github.com`. aiprov used to read any `<id>+Copilot@...` as the coding agent; it now splits them by id.
- The id in the email is not always right. One real commit has `197827+copilot-pull-request-reviewer[bot]@...` (yutto-dev/yutto, c40272a80c). So aiprov matches the account name, not the id. The one exception is `Copilot`, where the id is the only way to tell the two apart.
- Other tools show up in the same lines but are not review bots, so they are not added here: `google-labs-jules[bot]` (Jules, a coding agent), `devin-ai-integration[bot]` (Devin, a coding agent; names `Devin` and `Devin AI`), `Codex <codex@openai.com>` (seen once; not the `noreply@openai.com` form), `Copilot Autofix powered by AI <...>` (code scanning fixes), and `kilocode-bot[bot]`. Jules and Devin would be a good next addition as coding agents. Non-AI bots (`dependabot[bot]`, `github-actions[bot]`, `renovate[bot]`, `codecov[bot]`) are ignored.
- Some commit messages list many bots at once (for example Juwan-Hwang/Zephyr, 82bf4496a9). I did not use those as evidence for a bot. Only bots seen in a clear single-purpose commit are in the table.
- Bots I looked for but did not confirm in this pass (GitHub rate limit): Amazon Q Developer, Ellipsis, Korbit, Bito, Graphite, Codeflash, Sweep.
- Source for the mechanism: https://github.com/github/docs/blob/main/content/pull-requests/how-tos/review-pull-requests/incorporating-feedback-in-your-pull-request.md

## Standards used for output

- CycloneDX 1.6 JSON schema: https://raw.githubusercontent.com/CycloneDX/specification/1.6/schema/bom-1.6.schema.json
- SPDX 3.0.1 JSON schema: https://spdx.org/schema/3.0.1/spdx-json-schema.json and context https://spdx.org/rdf/3.0.1/spdx-context.jsonld
