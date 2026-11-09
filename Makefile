.PHONY: install run dev test build smoke
install:
	./scripts/install_omarchy.sh
run:
	.venv/bin/blackbox
dev:
	./scripts/dev.sh
test:
	.venv/bin/python -m pytest -q
build:
	npm --prefix frontend run build
smoke:
	./scripts/smoke_test.sh
