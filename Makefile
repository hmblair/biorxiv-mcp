VENV := $(CURDIR)/.venv
PYTHON := $(VENV)/bin/python
DEPLOY := $(CURDIR)/deploy
SYSTEMD_DIR := /etc/systemd/system
RUN_USER ?= $(USER)
SERVICE := biorxiv-mcp

# Client env: where the REST API lives and how to authenticate.
BIORXIV_API_URL ?= $(if $(BIORXIV_MCP_ENDPOINT),$(BIORXIV_MCP_ENDPOINT),http://localhost:8000)
BIORXIV_API_KEY ?= $(BIORXIV_MCP_ENDPOINT_KEY)

.PHONY: install uninstall install-agents uninstall-agents install-service uninstall-service start stop restart status test test-endpoint

# Register the stdio MCP shim with Claude Code, Claude Desktop, and OpenCode.
# Local:   make install
# Remote:  BIORXIV_MCP_ENDPOINT=https://biorxiv.example.com \
#          BIORXIV_MCP_ENDPOINT_KEY=<token> make install
install: $(VENV) install-agents
	python3 $(DEPLOY)/install_mcp.py install \
	    --url "$(BIORXIV_API_URL)" \
	    $(if $(BIORXIV_API_KEY),--key "$(BIORXIV_API_KEY)",)

uninstall:
	python3 $(DEPLOY)/install_mcp.py uninstall

CLAUDE_AGENTS_DIR := $(HOME)/.claude/agents

install-agents:
	mkdir -p $(CLAUDE_AGENTS_DIR)
	cp $(CURDIR)/agents/*.md $(CLAUDE_AGENTS_DIR)/
	@echo "Installed agents to $(CLAUDE_AGENTS_DIR)"

uninstall-agents:
	@for f in $(CURDIR)/agents/*.md; do \
		rm -f $(CLAUDE_AGENTS_DIR)/$$(basename $$f); \
	done
	@echo "Removed agents from $(CLAUDE_AGENTS_DIR)"

# Server-side: install systemd units (needs sudo).
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

test: $(VENV)
	$(VENV)/bin/pytest -q

test-endpoint: $(VENV)
	BIORXIV_MCP_ENDPOINT="$(BIORXIV_API_URL)" BIORXIV_MCP_ENDPOINT_KEY="$(BIORXIV_API_KEY)" \
	    $(VENV)/bin/pytest -v tests/test_endpoint.py

$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e '.[test]'
