# Semantic browser relay

The canonical quality and content contract is `../prompts/course-authoring-v2.md`.
The application assembles it verbatim with course guidance, approved plan and active
lecture specification, current task and an evidence attachment list.

1. Owner selects Continue in ChatGPT or Resume in ChatGPT.
2. Owner copies the semantic prompt and downloads the listed current evidence.
3. Owner opens a fresh ChatGPT conversation and attaches those files and prompt.
4. Executor returns semantic Markdown. Lecture generation returns one lecture only.
5. Owner pastes the text, optionally edits it, and submits it in the app.
6. Owner reviews the displayed priority basis and plan, edits/rejects/regenerates as
   needed, and explicitly approves. Only the app persists that approval.
7. Repeat one lecture at a time; build and download the final PDF in the app.

ChatGPT cloud cannot fetch evidence from `127.0.0.1`. The browser handles file
transfer. No API key or model-authored machine metadata is needed.

A stale-task diagnostic means reopen the current prompt. Syntax diagnostics mean
return corrected Markdown using the same semantic task. Preserve equations and
answers: do not disguise semantic correction as formatting repair. Invalid snippet
source/page diagnostics require a current valid source ID and page. Never silently
remove an invalid directive. Guidance is editable until generation starts, then
locked for that run. A different guidance basis requires a new course.

Existing pre-reset bare-ID plans remain preserved for local recovery, but the new
relay refuses to write lectures from insufficient plan context. Regenerate a rich
plan through explicit owner controls or create a new course. Old sidecar databases
remain untouched and are no longer read or written.
