#!/usr/bin/env bash
# PostToolUse hook on the Agent tool: remind Claude to arm the 30-minute "are agents stuck?" check.
# Owner's rule (CLAUDE.md, rule 0): every time agents are launched, a recurring check every 30 min must be running.
cat > /dev/null  # drain the hook payload
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Запущен агент. Правило проекта films (владелец): раз в 30 минут проверять, что фоновые агенты не зависли. Если таймер ещё не взведён — CronList; нет задачи проверки агентов → CronCreate (recurring, каждые 30 мин, напр. «13,43 * * * *») с промптом проверки: хвост tasks/*.output и прогресс с прошлой проверки; завис → SendMessage, затем TaskStop и перезапуск, запись в PLAN.md; короткий отчёт владельцу; когда фоновых агентов не осталось — CronDelete. Подробно: CLAUDE.md, правило 0."}}
JSON
