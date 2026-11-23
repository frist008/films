#!/usr/bin/env bash
# «Обнови базу», шаги без Chrome после сбора источников (скил update-db, шаги 3–8, 11а–11в; R313, R322, R324).
# Источники (rutor, rutracker, nnmclub, kinozal, rustorka, toloka, mazepa, Plex, Jellyfin) собираются раньше и отмечаются
# сами: `python -X utf8 -m films update --step <источник>`. Каждый удачный шаг здесь отмечается так же — `films update
# --finish` откажет, пока не отработали все шаги (R324: «ты меня обманул что всё закончил?»).
# Запуск: bash films/update_chain.sh   (START=N — продолжить с шага N после перезапуска).
# Лог: data/logs/agent_update_meta.log (PROGRESS N% ETA — шаг; в конце AGENT_UPDATE_META_OK), шаги — data/logs/update_<дата>_<шаг>.log.
# По умолчанию каждый шаг берёт только новое с точки последней синхронизации (R212, R322); повтор старого — только флагами
# по просьбе владельца (`--again`, `--all`, `--full-recheck`, …), здесь их нет.
cd "$(dirname "$0")/.." || exit 1
L=data/logs/agent_update_meta.log
D=$(date +%m%d)
START=${START:-1}
[ "$START" = 1 ] && : > "$L"
# имя для лога | шаг скила (films update --step) | команда
steps=(
  "enrich|enrich|python -X utf8 -m films enrich"
  "series_dates|series_dates|python -X utf8 -m films.enrich"
  "imdb_specs|imdb_specs|python -X utf8 -m films.sources.imdb_specs run"
  "ratings_imdb|ratings_imdb|python -X utf8 -m films ratings imdb"
  "ratings_rt|ratings_rt|python -X utf8 -m films ratings rt"
  "posters|posters|python -X utf8 -m films.posters run"
  "bluray_com|bluray_com|python -X utf8 -m films.sources.bluray_com run"
  "title_meta|title_meta|python -X utf8 -m films.title_meta all"
  "match_triage|match_triage|python -X utf8 -m films.match_triage all"
  "disks|disks|python -X utf8 -m films disks scan"
  "duplicates|duplicates|python -X utf8 -m films.duplicates"
  "qbittorrent|qbittorrent|python -X utf8 -m films.sources.qbittorrent"
  "qbittorrent_owned|qbittorrent_owned|python -X utf8 -m films.sources.qbittorrent owned"
  "watchlist|watchlist|python -X utf8 -m films.watchlist resolve"
  "health|health|python -X utf8 -m films.health"
  "franchise_resolve|franchise_resolve|python -X utf8 -m films.franchise resolve"
  "franchise_releases|franchise_releases|python -X utf8 -m films.franchise_releases"
  "games_collect|games_collect|python -X utf8 -m films.sources.games collect"
  "games_adaptations|games_adaptations|python -X utf8 -m films.sources.games adaptations"
  "games_report|games_report|python -X utf8 -m films.sources.games report"
  "prequels|prequels|python -X utf8 -m films.prequels collect"
  "quality_watch|quality_watch|python -X utf8 -m films.quality_watch"
  "encountered|encountered|python -X utf8 -m films.encountered collect"
  "rutor_pages|-|python -X utf8 -m films rutor pages"
  "rutor_parse|-|python -X utf8 -m films rutor parse"
  "kostya_relevant|kostya_relevant|python -X utf8 -m films.kostya_list relevant"
  "match|match|python -X utf8 -m films match"
  "enrich2|-|python -X utf8 -m films enrich"
  # titles made after the ratings step (prequels, encountered, match) — their ratings too; only new ones are taken
  # (R327, 2026-11-13: the sheet refused 7 titles without the IMDb rating and 32 without the RT check)
  "ratings_imdb2|-|python -X utf8 -m films ratings imdb"
  "ratings_rt2|-|python -X utf8 -m films ratings rt"
  "responsible|responsible|python -X utf8 -m films.responsible all"
  "fresh|fresh|python -X utf8 -m films.fresh"
  "trailers|trailers|python -X utf8 -m films.sources.youtube trailers"
  "trailer_audit|trailer_audit|python -X utf8 -m films.trailer_audit --apply"
  "trailer_check|trailer_check|python -X utf8 -m films.sources.trailer_lang check --unconfirmed"
  "trailer_fallbacks|trailer_fallbacks|python -X utf8 -m films.sources.trailer_lang fallbacks"
  "trailer_originals|trailer_originals|python -X utf8 -m films.sources.trailer_lang originals"
)
n=${#steps[@]}
t0=$(date +%s)
i=0
for s in "${steps[@]}"; do
  name=${s%%|*}; rest=${s#*|}; skill=${rest%%|*}; cmd=${rest#*|}
  if [ $((i + 1)) -lt "$START" ]; then i=$((i+1)); t0=$(date +%s); continue; fi
  pct=$(( i * 100 / n ))
  if [ "$i" -gt 0 ]; then
    el=$(( $(date +%s) - t0 )); eta=$(date -d "@$(( t0 + el * n / i ))" +%H:%M)
  else
    eta="?"
  fi
  echo "$(date '+%F %T') PROGRESS $pct% ETA $eta — шаг $((i+1))/$n: $name" >> "$L"
  ok=0
  for try in 1 2 3 4; do
    if $cmd >> "data/logs/update_${D}_$name.log" 2>&1; then ok=1; break; fi
    if grep -q "database is locked" "data/logs/update_${D}_$name.log"; then
      echo "$(date '+%F %T') retry $try ($name: database is locked)" >> "$L"; sleep 90
    else
      break
    fi
  done
  if [ "$ok" = 1 ]; then
    [ "$skill" = "-" ] || python -X utf8 -m films update --step "$skill" >> "$L" 2>&1
  else
    echo "$(date '+%F %T') STEP_FAILED $name (лог data/logs/update_${D}_$name.log)" >> "$L"
  fi
  i=$((i+1))
done
echo "$(date '+%F %T') PROGRESS 100% — цепочка закончена" >> "$L"
echo AGENT_UPDATE_META_OK >> "$L"
