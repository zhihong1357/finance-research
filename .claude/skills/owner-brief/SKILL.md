---
name: owner-brief
description: Answer the owner's status questions ("汇报", "现在怎么样", "我要做什么", "进度") with the fixed plain-Chinese owner brief defined in docs/owner-brief.md. Read-only; never starts workflows.
---

# Owner brief

When the owner asks how the project is going or what they need to do, reply with the owner brief.

1. Read `docs/owner-brief.md` and follow it exactly: what to check, how to set each traffic light, the five-part format, the rules.
2. Only read: the handoff page, the roadmap's current status, open pull requests and their checks, the last 24 hours of workflow runs, open issues. Do not start any workflow, read the vault, or touch the dataset.
3. Write in plain Chinese for a beginner, one screen long. Give a full link for everything the owner has to act on.
4. Report only. If the owner then asks for work, do it as a separate step under `CLAUDE.md`.
