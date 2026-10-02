# ViTrans — lệnh tiện dụng. `make check` là cửa vào duy nhất trước khi push.
PY ?= python
VENV ?= .venv
DOCS = docs

.PHONY: help install lint test test-db spec spec-build check clean

help:
	@grep -E '^[a-z-]+:' $(MAKEFILE_LIST) | sed 's/:.*//' | sort | xargs printf '  make %s\n'

install:
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check apps tests eval
	$(PY) -m ruff format --check apps tests eval

test:
	$(PY) -m pytest tests

# Test trên PostgreSQL thật: dùng pgserver (nhị phân nhúng) hoặc VISYNTH_TEST_DSN trỏ tới CSDL thử.
test-db:
	$(PY) -m pytest tests/test_db_schema.py tests/test_api_m1.py

spec:
	cd $(DOCS) && $(PY) tools/validate_spec.py
	cd $(DOCS) && $(PY) tools/check_spec_sync.py
	cd $(DOCS) && $(PY) -c "import yaml, openapi_spec_validator as v; v.validate(yaml.safe_load(open('api/openapi.yaml')))" && echo "OpenAPI hợp lệ"
	cd $(DOCS) && $(PY) -m pytest tests --ignore=tests/pg_smoke.py

# Dựng lại SPEC.md kèm kết quả kiểm tra tại chỗ — chạy trước khi commit khi sửa tools/spec_src/.
spec-build:
	cd $(DOCS) && $(PY) tools/build_spec.py --run-checks

check: lint test spec
