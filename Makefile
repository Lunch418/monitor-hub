.PHONY: help check lint types security test cov screenshot docker-test docker-matrix clean

PY ?= python3

help:            ## список команд
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ -/'

check: lint types security cov   ## все проверки

lint:            ## стиль и ошибки (ruff)
	ruff check .

types:           ## типы (mypy)
	mypy

security:        ## поиск небезопасных мест (bandit)
	bandit -q -c pyproject.toml -r monitor

test:            ## автотесты (включая ssh против поддельного шлюза)
	$(PY) -m pytest -q

cov:             ## автотесты с покрытием (минимум 85%)
	$(PY) -m pytest -q --cov

screenshot:      ## пересобрать docs/screenshot.svg
	$(PY) tools/make_screenshot.py docs/screenshot.svg

docker-test:     ## все проверки в контейнере
	docker build -q -t monitor-hub:test . >/dev/null
	docker run --rm monitor-hub:test make check

docker-matrix:   ## все проверки на Python 3.10, 3.11, 3.12 и 3.13
	@for v in 3.10 3.11 3.12 3.13; do \
	  echo "== Python $$v"; \
	  docker build -q --build-arg PYTHON_VERSION=$$v -t monitor-hub:py$$v . >/dev/null && \
	  docker run --rm monitor-hub:py$$v make check | grep -E "passed|failed|error" || exit 1; \
	done

clean:           ## убрать временные файлы
	rm -rf .pytest_cache .ruff_cache .mypy_cache .hypothesis .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
