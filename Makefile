VERSION := $(shell python3 -c "import sys; sys.path.insert(0,'src'); from myai_agent import __version__; print(__version__)" 2>/dev/null || echo "dev")
DIST    := dist

.PHONY: help test bundle-mac bundle-win clean

help:
	@echo "MyAI Agent $(VERSION) — build targets"
	@echo ""
	@echo "  make test          Run all tests"
	@echo "  make bundle-mac    Build macOS arm64 standalone binary via PyInstaller"
	@echo "  make bundle-win    Build Windows amd64 standalone binary via PyInstaller"
	@echo "  make clean         Remove dist/ and build/"

# ── Tests ──────────────────────────────────────────────────────────────────────

test:
	MYAI_ADMIN_SECRET=ci-stub-not-real python3 -m pytest tests/ -v

# ── Bundle targets ─────────────────────────────────────────────────────────────

# Common PyInstaller flags used by both platforms
PYINSTALLER_COMMON := \
	--onefile \
	--name myai-agent \
	--paths src \
	--hidden-import myai_agent.cli \
	--hidden-import myai_agent.agent \
	--hidden-import myai_agent.gpu \
	--hidden-import myai_agent.installer \
	--hidden-import myai_agent.attestation \
	--hidden-import myai_agent.config \
	--hidden-import websocket \
	src/myai_agent/__main__.py

bundle-mac:
	@echo "Building macOS arm64 bundle — myai-agent $(VERSION)"
	pip install --quiet "pyinstaller>=6" "websocket-client>=1.6"
	pyinstaller $(PYINSTALLER_COMMON) \
		--target-architecture arm64 \
		--distpath $(DIST)/mac
	@# Rename to versioned artifact
	mv $(DIST)/mac/myai-agent $(DIST)/mac/myai-agent-$(VERSION)-mac-arm64
	sha256sum $(DIST)/mac/myai-agent-$(VERSION)-mac-arm64 \
		> $(DIST)/mac/myai-agent-$(VERSION)-mac-arm64.sha256
	@echo "Done: $(DIST)/mac/myai-agent-$(VERSION)-mac-arm64"
	@cat $(DIST)/mac/myai-agent-$(VERSION)-mac-arm64.sha256

bundle-win:
	@echo "Building Windows amd64 bundle — myai-agent $(VERSION)"
	pip install --quiet "pyinstaller>=6" "websocket-client>=1.6"
	pyinstaller $(PYINSTALLER_COMMON) \
		--distpath $(DIST)/win
	@# Rename to versioned artifact (.exe extension for Windows)
	mv $(DIST)/win/myai-agent $(DIST)/win/myai-agent-$(VERSION)-win-amd64.exe 2>/dev/null || \
	mv $(DIST)/win/myai-agent.exe $(DIST)/win/myai-agent-$(VERSION)-win-amd64.exe
	sha256sum $(DIST)/win/myai-agent-$(VERSION)-win-amd64.exe \
		> $(DIST)/win/myai-agent-$(VERSION)-win-amd64.exe.sha256
	@echo "Done: $(DIST)/win/myai-agent-$(VERSION)-win-amd64.exe"
	@cat $(DIST)/win/myai-agent-$(VERSION)-win-amd64.exe.sha256

clean:
	rm -rf dist/ build/ *.spec
