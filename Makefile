PYTHON ?= python3
PREFIX ?= $(shell if [ "$$(id -u)" -eq 0 ]; then printf '%s' /usr/local; else printf '%s' "$$HOME/.local"; fi)
DESTDIR ?=
WHEELHOUSE ?=

INSTALLER = packaging/install_linux_source.py
INSTALL_ARGS = --source-root "$(CURDIR)" --prefix "$(PREFIX)" --destdir "$(DESTDIR)"
WHEELHOUSE_ARG = $(if $(strip $(WHEELHOUSE)),--wheelhouse "$(WHEELHOUSE)",)

.DEFAULT_GOAL := help
.PHONY: help install verify-install verify-gui print-install-layout uninstall

help:
	@printf '%s\n' \
	  'MBUprime StructLab Ubuntu source installation' \
	  '  make install              build, validate, and activate under PREFIX' \
	  '  make verify-install       run a live display-free installed CLI check' \
	  '  make verify-gui           run the installed Tk self-test (needs DISPLAY)' \
	  '  make print-install-layout show configured and staged paths' \
	  '  make uninstall            remove only this marked application version' \
	  '' \
	  'Variables: PREFIX=/absolute/path DESTDIR=/absolute/stage WHEELHOUSE=/absolute/dir PYTHON=python3.12'

install:
	$(PYTHON) $(INSTALLER) install $(INSTALL_ARGS) $(WHEELHOUSE_ARG)

verify-install:
	$(PYTHON) $(INSTALLER) verify $(INSTALL_ARGS)

verify-gui:
	$(PYTHON) $(INSTALLER) verify-gui $(INSTALL_ARGS)

print-install-layout:
	$(PYTHON) $(INSTALLER) print-layout $(INSTALL_ARGS)

uninstall:
	$(PYTHON) $(INSTALLER) uninstall $(INSTALL_ARGS)
