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
