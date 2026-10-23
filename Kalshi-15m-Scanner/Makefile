.PHONY: install test record report status

install:
	python -m pip install -r requirements-dev.txt

test:
	python -m compileall -q .
	pytest -q
	ruff check .

record:
	python main.py record

report:
	python main.py report

status:
	python main.py status
