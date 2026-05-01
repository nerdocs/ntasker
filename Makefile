.PHONY: install migrate run smoke clean

install:
	uv sync

migrate:
	uv run python migrate_todo.py

run:
	uv run uvicorn app:app --host 127.0.0.1 --port 8766 --reload

smoke:
	uv run python smoke_test.py

clean:
	rm -f tasks.db
