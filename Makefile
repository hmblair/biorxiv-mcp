VENV := $(CURDIR)/.venv
BIN := $(VENV)/bin/biorxiv-mcp
DEPLOY := $(CURDIR)/deploy
OPENCODE_CONFIG := $(HOME)/.config/opencode/opencode.json
SYSTEMD_USER_DIR := $(HOME)/.config/systemd/user
SERVICE := biorxiv-mcp

.PHONY: install uninstall install-service uninstall-service start stop restart status

install: $(VENV)
	claude mcp remove -s user $(SERVICE) 2>/dev/null || true
	claude mcp add -s user $(SERVICE) -- $(BIN)
	@python3 -c '\
import json; \
f = "$(OPENCODE_CONFIG)"; \
d = json.load(open(f)); \
d.setdefault("mcp", {})["$(SERVICE)"] = {"type": "local", "command": ["$(BIN)"]}; \
json.dump(d, open(f, "w"), indent=2); \
print("Added $(SERVICE) to opencode config")'

uninstall:
	claude mcp remove -s user $(SERVICE) || true
	@python3 -c '\
import json; \
f = "$(OPENCODE_CONFIG)"; \
d = json.load(open(f)); \
d.get("mcp", {}).pop("$(SERVICE)", None); \
json.dump(d, open(f, "w"), indent=2); \
print("Removed $(SERVICE) from opencode config")'

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
