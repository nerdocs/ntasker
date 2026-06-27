.PHONY: install init run smoke clean i18n-extract i18n-init-de i18n-update i18n-compile i18n

# Use the venv's Python / pybabel so we never accidentally run a system
# babel against this project (mismatched Python versions, missing deps).
PYTHON  := .venv/bin/python
PYBABEL := .venv/bin/pybabel

# Source of truth for the version, so pybabel embeds it in the .pot header.
VERSION := $(shell $(PYTHON) -c "import tomllib,pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")

install:
	uv sync

init:
	uv run ntasker init

# No --reload: a worker restart wipes the in-memory Claude PTY session
# registry (the master fd can't be re-adopted), silently killing any live
# session. Restart by hand (Ctrl-C + make run) when you change server code.
run:
	uv run ntasker serve

smoke:
	uv run python smoke_test.py
	uv run python lifespan_test.py

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


# ----------------------------------------------------------------------------
# i18n -- gettext + Babel workflow.
# Catalogs live at src/ntasker/locale/<lang>/LC_MESSAGES/ntasker.{po,mo}.
# Run `make i18n` after touching any translatable string in Python or
# Jinja templates. Default English source; German translation in de.po.
# ----------------------------------------------------------------------------

LOCALE_DIR := src/ntasker/locale
DOMAIN     := ntasker
POT        := $(LOCALE_DIR)/$(DOMAIN).pot

# `_lazy` and `t` are registered alongside the gettext defaults so
# pybabel-extract picks up `LazyString` defaults (e.g. HINTS values) and
# the Jinja shorthand `{{ t('...') }}` we use throughout templates.
i18n-extract:
	mkdir -p $(LOCALE_DIR)
	$(PYBABEL) extract -F babel.cfg \
	    -o $(POT) \
	    --copyright-holder=nerdocs --project=ntasker --version=$(VERSION) \
	    --keyword=_lazy --keyword=t --keyword=N_ \
	    src/ntasker

# Bootstrap a fresh German catalog. Idempotent: refuses to overwrite.
i18n-init-de:
	@if [ -f $(LOCALE_DIR)/de/LC_MESSAGES/$(DOMAIN).po ]; then \
		echo "de.po already exists -- use 'make i18n-update'."; \
	else \
		$(PYBABEL) init -i $(POT) -d $(LOCALE_DIR) -l de -D $(DOMAIN); \
	fi

# Merge new msgids from the .pot into existing .po catalogs.
i18n-update:
	$(PYBABEL) update -i $(POT) -d $(LOCALE_DIR) -D $(DOMAIN)

# Compile every .po into a .mo. Required before `uv build` so the wheel
# ships the binary catalogs.
i18n-compile:
	$(PYBABEL) compile -d $(LOCALE_DIR) -D $(DOMAIN)

# All-in-one: extract + update + compile. Run after touching any string.
i18n: i18n-extract i18n-update i18n-compile
