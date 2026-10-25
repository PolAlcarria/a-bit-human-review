---
name: findings
description: Code-review triage (a-bit-human-review) — open the latest review, list all reviews, show your saved triage, or check the setup
argument-hint: "[report.md | list | decisions | doctor]"
allowed-tools: Bash(env CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA} python3 ${CLAUDE_PLUGIN_ROOT}/finding_viewer.py:*)
---
!`env CLAUDE_PLUGIN_DATA=${CLAUDE_PLUGIN_DATA} python3 ${CLAUDE_PLUGIN_ROOT}/finding_viewer.py $ARGUMENTS`

If the output above is a triage summary, acknowledge it in one line and wait for the user's next instruction. If it is a doctor report (PASS / WARN / FAIL lines), show it unchanged in a code block. Otherwise relay it verbatim in one line. Do nothing else.
