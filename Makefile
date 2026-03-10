VENV := $(CURDIR)/.venv
PYTHON := $(VENV)/bin/python
SERVER := $(CURDIR)/server.py
OPENCODE_CONFIG := $(HOME)/.config/opencode/opencode.json

.PHONY: install uninstall

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

$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e .
