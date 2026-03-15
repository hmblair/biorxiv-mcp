VENV := $(CURDIR)/.venv
PYTHON := $(VENV)/bin/python
SERVER := $(CURDIR)/server.py
OPENCODE_CONFIG := $(HOME)/.config/opencode/opencode.json

SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user

.PHONY: install uninstall install-service uninstall-service

install: $(VENV)
	claude mcp remove --scope user biorxiv-mcp 2>/dev/null || true
	claude mcp add --scope user biorxiv-mcp -- $(PYTHON) $(SERVER)
	@python3 -c '\
import json; \
f = "$(OPENCODE_CONFIG)"; \
d = json.load(open(f)); \
d.setdefault("mcp", {})["biorxiv-mcp"] = {"type": "local", "command": ["$(PYTHON)", "$(SERVER)"]}; \
json.dump(d, open(f, "w"), indent=2); \
print("Added biorxiv-mcp to opencode config")'

uninstall:
	claude mcp remove --scope user biorxiv-mcp || true
	@python3 -c '\
import json; \
f = "$(OPENCODE_CONFIG)"; \
d = json.load(open(f)); \
d.get("mcp", {}).pop("biorxiv-mcp", None); \
json.dump(d, open(f, "w"), indent=2); \
print("Removed biorxiv-mcp from opencode config")'
	rm -rf $(VENV)

install-service: $(VENV)
	cp biorxiv-mcp.service $(SYSTEMD_USER_DIR)/
	cp biorxiv-sync.service $(SYSTEMD_USER_DIR)/
	cp biorxiv-sync.timer $(SYSTEMD_USER_DIR)/
	systemctl --user daemon-reload
	systemctl --user enable --now biorxiv-mcp.service
	systemctl --user enable --now biorxiv-sync.timer
	@echo ""
	@systemctl --user status biorxiv-mcp.service --no-pager || true

uninstall-service:
	systemctl --user disable --now biorxiv-mcp.service 2>/dev/null || true
	systemctl --user disable --now biorxiv-sync.timer 2>/dev/null || true
	rm -f $(SYSTEMD_USER_DIR)/biorxiv-mcp.service
	rm -f $(SYSTEMD_USER_DIR)/biorxiv-sync.service
	rm -f $(SYSTEMD_USER_DIR)/biorxiv-sync.timer
	systemctl --user daemon-reload

$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e .
