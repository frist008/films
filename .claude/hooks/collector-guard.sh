#!/usr/bin/env bash
# PreToolUse: один сайт — один сборщик в Chrome (владелец, 2026-11-23). Логика — collector_guard.py.
python -X utf8 "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/collector_guard.py"
