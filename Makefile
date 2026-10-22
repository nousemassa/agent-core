# Общий слой (`agent_core`): линтер, офлайн-тесты, гейт схемы и сборка колеса.
.PHONY: ci lint test schema-check wheel clean

#: Интерпретатор с окружением проекта.
PY ?= python3

#: Цель `ci`: линтер по своим файлам и свои тесты. Живые тесты пропускаются: им нужна платформа.
ci: lint test

lint:
	$(PY) -m ruff check .

test:
	$(PY) -m pytest tests -q -m "not live"

# Гейт схемы: накат DDL на ЧИСТУЮ базу и сверка с ОЖИДАЕМЫМ. Код берётся из реестра компонентов —
# копии DDL в скрипте нет. DSN чистой базы: `CI_DB_URL`, иначе `DB_URL`, иначе `BRIDGE_DB_URL`.
schema-check:
	$(PY) scripts/ci-schema-check.py --components core,runtime

# Колесо для потребителей. `dist/` в git не попадает: продукт ставит точную версию из своего пина.
wheel:
	$(PY) -m build --wheel . --outdir dist

# Сборка переиспользует `build/` и `*.egg-info/`: устаревший список пакетов вернул бы в колесо то,
# что из него исключено. Поэтому перед сборкой — чисто.
clean:
	rm -rf build dist *.egg-info
