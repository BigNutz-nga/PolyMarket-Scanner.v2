.PHONY: install test scan validate bot

install:
	python -m pip install -r requirements-dev.txt

test:
	python -m compileall -q .
	pytest -q
	ruff check .

scan:
	python main.py --refresh

validate:
	python live_validate.py --output-dir validation

bot:
	python bot.py
