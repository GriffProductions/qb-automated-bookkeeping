# Browser renaming pilot — how to run

EASY MODE (recommended): each `files/batch_NN/` folder holds that batch's PDFs
as DOC-01.pdf … (neutral names, copied from your drives). Per batch: open the
folder, Ctrl+A, drag into ONE gemini.google.com message, paste the batch .md.

LAZY MODE (no attaching): just paste each batch .md — document text is inline.
Works for everything except the 1–2 docs marked NEEDS-its-PDF-attached (scans).

1. Paste `00_setup_prompt.md` as the first message.
2. Six batch messages (easy or lazy mode).
3. Copy ALL of Gemini's replies into `gemini_replies.md` in this folder.
4. Score: `python -m qb_automation.main score-testkit --kit data/browser_test_kit --replies data/browser_test_kit/gemini_replies.md`

NEVER paste `answer_key.json` into the chat — it is the hidden answer sheet.
