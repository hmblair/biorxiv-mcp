VENV := $(CURDIR)/.venv
PYTHON := $(VENV)/bin/python
DEPLOY := $(CURDIR)/deploy
SYSTEMD_DIR := /etc/systemd/system
# User that the system service runs as (defaults to whoever ran `make`).
RUN_USER ?= $(USER)
SERVICE := biorxiv-mcp
PORT ?= 8000
MCP_URL ?= http://localhost:$(PORT)/mcp
# Optional: full Authorization header value, e.g. MCP_AUTH="Bearer <token>"
MCP_AUTH ?=

.PHONY: install uninstall install-service uninstall-service start stop restart status

# Register the running HTTP MCP with Claude Code, Claude Desktop, and OpenCode.
# For a remote server with auth:
#   make install MCP_URL=https://biorxiv.example.com/mcp MCP_AUTH="Bearer $$KEY"
install:
	python3 $(DEPLOY)/install_mcp.py install --name $(SERVICE) --url $(MCP_URL) \
	    $(if $(MCP_AUTH),--auth "$(MCP_AUTH)",)

uninstall:
	python3 $(DEPLOY)/install_mcp.py uninstall --name $(SERVICE)

# install-service requires sudo to write under /etc/systemd/system.
install-service: $(VENV)
	sed -e 's|@PROJECT_ROOT@|$(CURDIR)|g' -e 's|@RUN_USER@|$(RUN_USER)|g' \
	    $(DEPLOY)/$(SERVICE).service.in | sudo tee $(SYSTEMD_DIR)/$(SERVICE).service > /dev/null
	sed -e 's|@PROJECT_ROOT@|$(CURDIR)|g' -e 's|@RUN_USER@|$(RUN_USER)|g' \
	    $(DEPLOY)/biorxiv-sync.service.in | sudo tee $(SYSTEMD_DIR)/biorxiv-sync.service > /dev/null
	sudo cp $(DEPLOY)/biorxiv-sync.timer $(SYSTEMD_DIR)/
	sudo systemctl daemon-reload
	sudo systemctl enable --now $(SERVICE).service
	sudo systemctl enable --now biorxiv-sync.timer
	@echo ""
	@systemctl status $(SERVICE).service --no-pager || true

uninstall-service:
	sudo systemctl disable --now $(SERVICE).service 2>/dev/null || true
	sudo systemctl disable --now biorxiv-sync.timer 2>/dev/null || true
	sudo rm -f $(SYSTEMD_DIR)/$(SERVICE).service
	sudo rm -f $(SYSTEMD_DIR)/biorxiv-sync.service
	sudo rm -f $(SYSTEMD_DIR)/biorxiv-sync.timer
	sudo systemctl daemon-reload

start:
	sudo systemctl start $(SERVICE)

stop:
	sudo systemctl stop $(SERVICE)

restart:
	sudo systemctl restart $(SERVICE)

status:
	@systemctl status $(SERVICE) --no-pager || true

$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e .
