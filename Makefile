.PHONY: install init run smoke clean

install:
	uv sync

init:
	uv run ntasker init

run:
	uv run ntasker serve --reload

smoke:
	uv run python smoke_test.py

# Removes ONLY the local repo-root tasks.db (legacy / dev artefacts).
# The default user-data DB at ``platformdirs.user_data_dir('nTasker')/tasks.db``
# is intentionally left alone -- ``make clean`` is for repo hygiene, not for
# nuking real data. Remove the user-data DB by hand if you really want to.
clean:
	@if [ -f tasks.db ]; then \
		echo "Removing local repo-root tasks.db (the user-data DB stays)."; \
		rm -f tasks.db; \
	else \
		echo "No local tasks.db to remove."; \
	fi
