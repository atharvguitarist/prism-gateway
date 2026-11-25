PY ?= python3
.PHONY: install test run mocks demo verify docker-up docker-down routing-eval

install:        ## create venv + install deps
	$(PY) -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

test:           ## unit + integration tests (no network, fake providers)
	.venv/bin/python -m pytest -q

mocks:          ## start both mock providers in the background
	$(PY) scripts/mock_provider.py --port 9001 --name alpha & $(PY) scripts/mock_provider.py --port 9002 --name beta &

run:            ## gateway on :8080 (needs the mocks)
	.venv/bin/uvicorn prism.app:app --factory --port 8080

routing-eval:   ## one-command routing eval (provided set + held-out set)
	.venv/bin/python -m prism.routing_eval && .venv/bin/python -m prism.routing_eval --file data/routing_eval_heldout.jsonl

demo:           ## the 10-step demo flow, press Enter between steps
	$(PY) scripts/demo.py --pause

verify:         ## boot everything on fresh state, run all checks, regenerate VERIFICATION_REPORT.md
	.venv/bin/python scripts/verify.py

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down -v
