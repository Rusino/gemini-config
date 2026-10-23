---
name: redline
description: >-
  Generate a rendered-Markdown HTML redline (`<stem>_diff.html`) with green `<ins>` and red
  strikethrough `<del>` between two versions of a document (files, git revisions, transcript
  snapshots, or Google Docs) and verify via headless browser screenshot. Trigger after editing a
  document the user already read, or when asked to "покажи изменения", "что поменялось",
  "покажи дифф документа", "зелёным и красным зачёркнутым", or "redline".
---

# Document Redline Skill (`redline`)

When iterating on a design doc, review response, specification, roadmap, or skill file that the
user has already read, forcing the user to re-read the entire file or inspect a raw unified
`git diff` of wrapped Markdown lines wastes attention.

This skill produces a standalone, browser-ready HTML document (`<stem>_diff.html`) that:
1. **Renders Markdown structure** (`<h1>`–`<h6>`, `<ul>`, `<ol>`, `<li>` with preserved numbering
   and nesting, `> [!NOTE]` alerts, `<blockquote>`, `<pre><code>`, `<table>`, `<strong>`, `<em>`,
   `<code>`, `<a>`) rather than dumping raw `##` or backticks.
2. **Highlights additions in green (`<ins>`) and removals in red strikethrough (`<del>`)** at word
   granularity while keeping inline code spans (`` `symbol` ``) atomic.
3. **Coalesces small edits** separated by at most 4 unchanged words (configurable via
   `--coalesce-words`) into a single `<del>было</del> <ins>стало</ins>` phrase block so sentences
   never turn into alternating word salad, while falling back to a clean stacked `Было / Стало`
   block whenever more than 55% of a paragraph's words are rewritten.
4. **Detects moved blocks (`Move Detection`)** across sections in a second pass, pairing relocated
   paragraphs or list items (`similarity >= 0.50`) so only the actual word changes inside the moved
   block are highlighted at the new position (`↕ Перенесено из «...»`) while leaving a compact
   1-line link stub (`↪ Перенесено в «...»`) at the old position.
5. **Defaults the comparison base to the last version the user saw** (`turn_boundary` in
   `transcript_full.jsonl` or `transcript.jsonl`, falling back to `git show HEAD:<path>` or
   `HEAD~1:<path>`).
6. **Includes a sticky top bar** with word-count delta (`was -> now`), changed-block count, base
   version label, and one-click view modes (`Было → Стало`, `Две колонки (Было | Стало)`,
   `Фокус на правках`, `Чистый итог`, `База`), automatically opening in `Две колонки` mode when
   more than 50% of the document's words changed.

> [!NOTE]
> For external collaboration directly inside Google Docs where reviewers use native Google Docs
> comments or suggestions, prefer Google Docs' built-in *Suggesting* mode or
> *Tools → Compare documents*. Use `redline` when reviewing drafts, shortening or translating
> documents, or comparing local Markdown, Git, transcript, or Drive snapshots with the user.

---

## Step 1: Generate the HTML Redline via `skills/redline/scripts/redline.py`

Run `skills/redline/scripts/redline.py` (from `~/.gemini/config`):

### Common Invocation Modes

1. **Single file (automatic base = last version the user saw in transcript, or Git `HEAD`)**:
   ```bash
   python3 ~/.gemini/config/skills/redline/scripts/redline.py \
     <artifact_dir>/draft.md \
     -o <artifact_dir>/draft_diff.html
   ```
   - If `<artifact_dir>/draft.md` is inside `<appDataDir>/brain/<conv_id>/...` (or
     `--chat <conv_id>` or `ANTIGRAVITY_CONVERSATION_ID` is provided),
     `skills/redline/scripts/redline.py` replays `write_file`/`write_to_file`,
     `edit_file`/`replace_file_content`, and full `read_file`/`view_file` steps from
     `transcript_full.jsonl` and selects the most recent `USER_INPUT` turn-boundary snapshot whose
     text differs from the current file.
   - If the file has no earlier transcript snapshot and lives inside a Git repository,
     `skills/redline/scripts/redline.py` automatically compares against `HEAD` (when the working
     tree is dirty) or `HEAD~1` (when clean).

2. **Two explicit files (e.g., full draft vs shortened draft)**:
   ```bash
   python3 ~/.gemini/config/skills/redline/scripts/redline.py \
     <artifact_dir>/old_draft.md \
     <artifact_dir>/new_draft.md \
     -o <artifact_dir>/draft_diff.html
   ```

3. **Explicit transcript step or turn snapshot**:
   - Inspect all historical snapshots of a file in a conversation:
     ```bash
     python3 ~/.gemini/config/skills/redline/scripts/redline.py \
       --list-snapshots <artifact_dir>/draft.md --chat <conv_id>
     ```
   - Compare against a specific transcript step or turn offset:
     ```bash
     python3 ~/.gemini/config/skills/redline/scripts/redline.py \
       <artifact_dir>/draft.md --chat <conv_id> --base-step 348 \
       -o <artifact_dir>/draft_diff.html

     python3 ~/.gemini/config/skills/redline/scripts/redline.py \
       @step:348:<artifact_dir>/draft.md \
       @step:370:<artifact_dir>/draft.md \
       -o <artifact_dir>/draft_diff.html
     ```

4. **Git revisions**:
   ```bash
   python3 ~/.gemini/config/skills/redline/scripts/redline.py \
     --git HEAD~1 skills/redline/SKILL.md \
     -o <artifact_dir>/skill_diff.html

   python3 ~/.gemini/config/skills/redline/scripts/redline.py \
     HEAD~1:skills/redline/SKILL.md HEAD:skills/redline/SKILL.md \
     -o <artifact_dir>/skill_diff.html
   ```

5. **Google Doc and Drive snapshots**:
   - Never use the retired `gemini-agents-gdrive` or `gemini-agents-gdocs` CLIs.
   - `skills/redline/scripts/redline.py` directly accepts JSON files saved from
     `google_drive_google_drive` (`read_file_content`) or `google_docs_google_docs` (`read_doc`),
     automatically unescaping Drive Markdown escapes (`\_`, `\~`, `\<`, `\>`) and converting Docs
     structural JSON (`tabs[0].documentTab.body.content`) into Markdown:
     ```bash
     python3 ~/.gemini/config/skills/redline/scripts/redline.py \
       --gdoc <documentId> <artifact_dir>/new_draft.md --chat <conv_id> \
       -o <artifact_dir>/draft_diff.html
     ```

---

## Step 2: Mandatory Headless Browser Visual Verification

Before presenting the generated `.html` file to the user, verify that it renders cleanly with no
broken tags, raw Markdown artifacts, or console errors:

1. **Navigate**: Call `browser_navigate` (`ServerName="browser_browser"`) with
   `url="file://<artifact_dir>/draft_diff.html"`, `wait_for_selector="#redline-root"`,
   `wait_for_network_idle=true`.
2. **Telemetry check**: Call `browser_get_console_logs` and `browser_get_network_activity`
   immediately after navigation to confirm zero JS errors.
3. **Screenshot & inspect**: Call `browser_capture_screenshot` saving to
   `<artifact_dir>/draft_diff_shot.png` (`full_page=false`), then view the PNG with
   `read_file` (or `view_file`) to verify that:
   - Headings, bullet and numbered lists, alerts, and inline `<code>` blocks are rendered as HTML
     (not raw `##`, `**`, or backticks).
   - Green `<ins>` and red strikethrough `<del>` spans are legible and properly coalesced.
4. **Deliver**: Link the `.html` file in chat
   (`[draft_diff.html](file://<artifact_dir>/draft_diff.html)`) and briefly state which base
   version was used and the word-count delta.
