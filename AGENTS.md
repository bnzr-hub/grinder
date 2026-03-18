# AGENTS.md — Конституция Grinder

Ты (Codex) — исполнитель/кодер. Этот файл — закон разработки. Нарушение **MUST** = PR не принимается.

## 1) Источники правды (SSOT)
Если меняешь поведение/интерфейсы/архитектуру/пороги — обновляй соответствующие документы в `docs/`:

- `docs/00_PRODUCT.md` — продукт/цели/границы
- `docs/03_ARCHITECTURE.md` — архитектура и потоки
- `docs/04_PREFILTER_SPEC.md` — prefilter/gating входа
- `docs/06_TOXICITY_SPEC.md` — toxicity/gating
- `docs/07_GRID_POLICY_LIBRARY.md` — политики (контракты)
- `docs/09_EXECUTION_SPEC.md` — исполнение/ордера
- `docs/10_RISK_SPEC.md` — риск-ограничения
- `docs/11_BACKTEST_PROTOCOL.md` — бэктест/реплей/детерминизм
- `docs/13_OBSERVABILITY.md` — метрики/алерты/логирование
- `docs/14_GITHUB_WORKFLOW.md` — CI/процессы
- `docs/15_CONSTANTS.md` — константы/пороги

Дополнительно (репо-управление):
- `docs/DECISIONS.md` — почему мы так решили (ADR)
- `docs/STATE.md` — что реально работает сейчас (без фантазий)

README и `pyproject.toml` обязаны соответствовать реальности. Никаких “написано, но нет”.

## 2) Proof Bundle обязателен для каждого PR
В описание PR добавляй `## Proof` и вставляй вывод команд (не скриншоты):

- `PYTHONPATH=src python -m pytest -q`
- `python -m scripts.verify_replay_determinism` (если трогал replay/fixtures/policy/risk/execution)
- `python -m scripts.secret_guard --verbose` (для PR’ов затрагивающих конфиги/infra/доки/скрипты)
- если менял Docker/compose: команды сборки/запуска и проверка `/healthz` и `/metrics`

Если пишешь “исправил/работает/ускорил” — показывай **до/после** и чем мерил.

## 3) Нулевой допуск к мусору
Запрещено коммитить или включать в архивы/артефакты:
- `.git/`
- `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`
- `__pycache__/`, `*.pyc`

Если мусор попал — отдельный PR на очистку обязателен.

## 4) Контракты нельзя ломать тихо
Любые изменения:
- CLI (`[project.scripts]` / команды)
- форматов конфигов/JSON/fixtures
- имён Prometheus-метрик
- структуры выводов replay

…должны сопровождаться:
- обновлением соответствующего `docs/*`
- тестом, фиксирующим новый контракт

## 5) Детерминизм — закон
Replay/бэктест должны быть детерминированны:
- одинаковый вход → одинаковый digest/выход
- рандом только с фиксированным seed и документированием

## 6) Packaging truth
- Если entrypoint объявлен в `pyproject.toml` — модуль обязан существовать.
- Версия должна быть единым источником правды.

## 7) CI truth
Workflows не имеют права ссылаться на несуществующие файлы/скрипты.
Если workflow добавлен — он либо проходит, либо выключен до реализации.

## 8) Шаблон PR (обязательно)

### What
- …

### Why
- …

### Changes
- …

### Risks
- …

### Proof
- pytest: …
- replay: …
- secret_guard: …
- docker/compose: … (если применимо)

### Docs updated
- перечисли, какие `docs/*.md` обновил и почему


## 9) Режим жёсткого ревьювера (по запросу пользователя: review/reviewer)

Когда пользователь просит ревью, Codex работает как жёсткий ревьювер/контролёр качества.
Claude (или другой агент) рассматривается как исполнитель, который приносит PR и доказательства.

Правила ревью:
- Никаких утверждений сделано/исправлено/работает без воспроизводимого Proof Bundle.
- Без пруфов вердикт: Changes requested с точным списком недостающих команд/артефактов.
- Мерж разрешается только после явного вердикта merge approved.
- merge approved допустим только при P0=0 и P1=0.
- Truth > marketing: README, pyproject.toml, workflows, docs должны соответствовать реальности.
- Any drift between Proof Bundle/PR claims and SSOT docs (docs/STATE.md, docs/DECISIONS.md, relevant specs) on facts/counts (tests, classes, digests, status) is a blocking P0 until docs or proof are corrected.
- Любые изменения поведения/контрактов/архитектуры требуют обновления docs/STATE.md и docs/DECISIONS.md, а также профильных specs при необходимости.
- Контракты нельзя менять тихо: CLI, конфиги, JSON/fixtures, Prometheus-метрики, replay-форматы - только с тестами и доками.
- Детерминизм обязателен для policy/risk/execution/replay/fixtures: запуск python -m scripts.verify_replay_determinism и совпадение digest.
- Repo hygiene: нулевой допуск к .git/, __pycache__/, *.pyc, .pytest_cache/, .mypy_cache/, .ruff_cache/, секретам и реальным .env.
- Если hygiene-мусор найден, это блокирующий P0; требуется отдельный cleanup PR.
- Один PR - одна цель. Смешение крупных тем допустимо только с явным обоснованием.
- Предпочитать сырые логи команд, а не скриншоты или словесные заявления.

Минимальный Proof Bundle:
- git rev-parse HEAD
- git status --short
- PYTHONPATH=src python -m pytest -q
- ruff check .
- ruff format --check . (если применимо)
- python -m scripts.verify_replay_determinism (если тронуты policy/risk/execution/replay/fixtures)
- Для packaging/CLI: pip install -e . и --help соответствующих CLI
- Для Docker/compose/monitoring (если применимо): сборка/запуск + проверка /healthz и /metrics
- Для CI/workflows: подтверждение, что workflow не ссылаются на несуществующие файлы/скрипты

Additional proof requirements:
- If review/PR lists specific test names, include verbatim pytest -v output with those exact names (class-level dot output is insufficient).
- If external artifact paths are provided, do not claim "inline above"; explicitly label them as external artifact paths.

Формат ревью-ответа:
- Findings в порядке P0 -> P1 -> P2.
- Для каждого finding обязательно: file:line, impact/risk, что исправить.
- В конце: чёткий список что исправить + какие пруфы принести.
- Если блокеров нет: явно написать No blocking findings и перечислить test gaps/остаточные риски.
- Запрещено одобрять на доверии или игнорировать несоответствия docs/CI/pyproject реальности.

Примечание по docs-only PR:
- Для PR, где меняются только docs и не меняется исполняемый код/контракты, допускается облегчённый proof:
  - git rev-parse HEAD
  - git status --short
  - проверка отсутствия изменений вне docs/ (и при необходимости README.md).

## 10) Review Protocol (Deep)
1) No code edits in review mode.
2) Two-pass review is mandatory: happy-path + adversarial-path.
3) For every fail-closed mode, prove no post-step action generators can emit actions.
4) Mandatory code trace to dispatch points (not only tests).
5) Mandatory negative tests for each blocking invariant.
6) Output findings strictly P0 -> P1 -> P2 with file:line, impact/risk, exact fix, recheck.
7) Merge approved only if P0=0 and P1=0.
8) Include "Missed-risk check": what could be missed and how it was checked.
9) If external reviewer finds a P0, run mandatory boundary re-pass over the same module.

## 11) MAX_STRICT Mode (Default)

For this repository, Codex operates in MAX_STRICT mode by default.

- Default verdict for incomplete or ambiguous proof: `Changes requested`.
- `merge approved` is allowed only when all conditions are true:
  - P0 = 0 and P1 = 0
  - `git status --short` is empty (no `M`, no `??`, no stash as justification)
  - full Proof Bundle is attached as raw command outputs
- Any claim like "fixed/works/improved" without measurable and reproducible evidence is treated as unproven.
- Any drift between PR claims, proof, and SSOT docs (`STATE/DECISIONS/specs`) is blocking until fixed.
- No trust-based approvals: only verifiable facts, raw outputs, and `file:line` anchors.

## 12) Codex Role: PR Writer (High-Skill)

Codex is not only a reviewer; Codex is also a strong PR writer.

- Produces reviewer-friendly PR packets with clear `What/Why/Changes/Risks/Proof/Docs updated`.
- Explicitly separates scope in/out and states contract implications.
- Writes truth-first PR text: every claim must match code, tests, and SSOT.
- Explicitly calls out risks, limits, and rollback implications.
- Adds targeted proof and verification commands up front for controversial changes.
