VENV := $(CURDIR)/.venv
PYTHON := $(VENV)/bin/python
DEPLOY := $(CURDIR)/deploy
SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user
SERVICE := biorxiv-mcp
PORT ?= 8000
MCP_URL ?= http://localhost:$(PORT)/mcp

.PHONY: install uninstall install-service uninstall-service start stop restart status

# Register the running HTTP MCP with Claude Code, Claude Desktop, and OpenCode.
install:
	$(PYTHON) $(DEPLOY)/install_mcp.py install --name $(SERVICE) --url $(MCP_URL)

uninstall:
	$(PYTHON) $(DEPLOY)/install_mcp.py uninstall --name $(SERVICE)

install-service: $(VENV)
	sed 's|@PROJECT_ROOT@|$(CURDIR)|g' $(DEPLOY)/$(SERVICE).service.in > $(SYSTEMD_USER_DIR)/$(SERVICE).service
	sed 's|@PROJECT_ROOT@|$(CURDIR)|g' $(DEPLOY)/biorxiv-sync.service.in > $(SYSTEMD_USER_DIR)/biorxiv-sync.service
	cp $(DEPLOY)/biorxiv-sync.timer $(SYSTEMD_USER_DIR)/
	systemctl --user daemon-reload
	systemctl --user enable --now $(SERVICE).service
	systemctl --user enable --now biorxiv-sync.timer
	@echo ""
	@systemctl --user status $(SERVICE).service --no-pager || true

uninstall-service:
	systemctl --user disable --now $(SERVICE).service 2>/dev/null || true
	systemctl --user disable --now biorxiv-sync.timer 2>/dev/null || true
	rm -f $(SYSTEMD_USER_DIR)/$(SERVICE).service
	rm -f $(SYSTEMD_USER_DIR)/biorxiv-sync.service
	rm -f $(SYSTEMD_USER_DIR)/biorxiv-sync.timer
	systemctl --user daemon-reload

start:
	systemctl --user start $(SERVICE)

stop:
	systemctl --user stop $(SERVICE)

restart:
	systemctl --user restart $(SERVICE)

status:
	@systemctl --user status $(SERVICE) --no-pager || true

$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e .
