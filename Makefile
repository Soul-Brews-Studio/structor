# Structor — build / test / run / deploy
#
#   make build        Go server (bin/structor) + Rust CLI (bin/structor-cli)
#   make test         go test + cargo test (+ swift build of the tray)
#   make run          serve on 127.0.0.1:8091 with a local superuser
#   make scan         one CLI pass over ~/.claude/projects into the local server
#   make watch        follow ~/.claude/projects
#   make tray         build + launch the macOS menu-bar app
#   make lance-install  bun install for the LanceDB replica (app/lance)
#   make lance        run the LanceDB replica + admin in the foreground (127.0.0.1:8092)
#   make lance-once   one sync pass into lance_data, then exit
#   make lance-typecheck  tsc --noEmit over app/lance
#   make lance-py-install  uv sync for the Python edition (app/lance-py)
#   make lance-py       run the Python replica + admin in the foreground (127.0.0.1:8094)
#   make lance-py-once  one sync pass into lance_data_py, then exit
#   make lance-py-test  ruff + pytest over app/lance-py
#   make dream-install  uv sync for structor-dream (app/dream)
#   make dream-test     ruff + pytest over app/dream (no GPU)
#   make dream-nightly  one nightly dream pass in the foreground — what launchd runs at 03:30
#   make record-live  record the live page as recordings/<NAME>.webm + .gif (URL= SECONDS= FPS= OUT= NAME=)
#   make linux        static linux/amd64 + linux/arm64 server binaries for HAOS
#   make deploy       rsync the add-on to kvmlab1:/addons/structor and (re)install it
#
# Go, Rust, Bun and uv toolchains live outside PATH on this machine; override if yours differ.
GO      ?= $(HOME)/sdk/go/bin/go
CARGO   ?= $(HOME)/.rustup/toolchains/stable-aarch64-apple-darwin/bin/cargo
SWIFT   ?= swift
BUN     ?= $(HOME)/.bun/bin/bun
UV      ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS  = -s -w -X main.Version=$(VERSION)

# 8090 is the add-on's port on kvmlab1; locally 8091 avoids whatever else sits on 8090.
HTTP     ?= 127.0.0.1:8091
DATA_DIR ?= $(CURDIR)/pb_data
export STRUCTOR_ADMIN_EMAIL    ?= admin@structor.local
export STRUCTOR_ADMIN_PASSWORD ?= structor-dev-password
export STRUCTOR_TZ             ?= Asia/Bangkok

GUEST ?= kvmlab1
SLUG   = structor

.PHONY: build build-go build-cli test test-go test-cli test-tray lance-typecheck lance-py-test dream-test run scan watch status tray tray-app install-tray lance-install lance lance-once lance-py-install lance-py lance-py-once dream-install dream-nightly record-live install-agents uninstall-agents agents-status linux deploy deploy-files clean

build: build-go build-cli

build-go:
	@mkdir -p bin
	$(GO) build -ldflags "$(LDFLAGS)" -o bin/structor .

build-cli:
	@mkdir -p bin
	cd cli && PATH="$(dir $(CARGO)):$$PATH" $(CARGO) build --release
	# new inode on purpose: cp over a binary a running watcher has mapped
	# invalidates macOS's cached code signature and every new launch dies
	# with SIGKILL (exit 137)
	rm -f bin/structor-cli && cp cli/target/release/structor-cli bin/structor-cli.new && mv bin/structor-cli.new bin/structor-cli

test: test-go test-cli test-tray lance-typecheck lance-py-test dream-test

test-go:
	$(GO) vet ./... && $(GO) test ./...

test-cli:
	cd cli && PATH="$(dir $(CARGO)):$$PATH" $(CARGO) test

test-tray:
	cd tray && $(SWIFT) build 2>&1 | tail -3

lance-typecheck:
	@if [ -x "$(BUN)" ] && [ -d lance/node_modules ]; then cd lance && $(BUN) x tsc --noEmit; \
	else echo "lance-typecheck: bun or lance/node_modules missing, skipped (make lance-install)"; fi

run: build-go
	./bin/structor serve --http=$(HTTP) --dir=$(DATA_DIR)

scan: build-cli
	./bin/structor-cli --url http://$(HTTP) --email $(STRUCTOR_ADMIN_EMAIL) --password $(STRUCTOR_ADMIN_PASSWORD) scan

watch: build-cli
	./bin/structor-cli --url http://$(HTTP) --email $(STRUCTOR_ADMIN_EMAIL) --password $(STRUCTOR_ADMIN_PASSWORD) watch

status:
	./bin/structor-cli --url http://$(HTTP) --email $(STRUCTOR_ADMIN_EMAIL) --password $(STRUCTOR_ADMIN_PASSWORD) status

tray:
	cd tray && $(SWIFT) build -c release && ./.build/release/StructorTray &

# .app bundle (LSUIElement, ad-hoc signed) — tray-app builds it under tray/.build,
# install-tray also copies it to /Applications and relaunches it
tray-app:
	./scripts/bundle-tray.sh

install-tray:
	./scripts/bundle-tray.sh install

# LanceDB replica of the PocketBase store plus its admin UI (Bun; app/lance).
# It reads ~/.config/structor/*.json for the targets, so no credentials here.
# best effort: a Mac without bun still gets the other agents
lance-install:
	@if [ -x "$(BUN)" ]; then cd lance && $(BUN) install; \
	else echo "lance-install: bun not found at $(BUN) — skipping (install bun, then make lance-install)"; fi

lance:
	cd lance && $(BUN) src/main.ts

lance-once:
	cd lance && $(BUN) src/main.ts --once

# The same replica in Python (app/lance-py): ORM-style schema, its own port
# (8094) and its own data directory (lance_data_py/), so both editions can run
# side by side. uv owns the venv; a Mac without uv still gets everything else.
lance-py-install:
	@if [ -x "$(UV)" ]; then $(UV) sync --project lance-py; \
	else echo "lance-py-install: uv not found at $(UV) — skipping (install uv, then make lance-py-install)"; fi

lance-py:
	$(UV) run --project lance-py structor-lance serve

lance-py-once:
	$(UV) run --project lance-py structor-lance once

lance-py-test:
	@if [ -x "$(UV)" ] && [ -d lance-py/.venv ]; then cd lance-py && $(UV) run ruff check src tests && $(UV) run pytest -q; \
	else echo "lance-py-test: uv or lance-py/.venv missing, skipped (make lance-py-install)"; fi

# Dream pages (app/dream): model-generated weekly / topic insight notes over the
# Python replica, written into the wiki so `ask` can cite them. Same uv-owned
# venv pattern as lance-py, no port and no daemon: launchd runs `dream-nightly`
# once a day at 03:30. Config (chat host, pool, wiki_dir, dream_dir) is read
# from ~/.config/structor/lance.json by structor-dream itself.
dream-install:
	@if [ -x "$(UV)" ]; then $(UV) sync --project dream; \
	else echo "dream-install: uv not found at $(UV) — skipping (install uv, then make dream-install)"; fi

dream-nightly:
	$(UV) run --project dream structor-dream nightly

dream-test:
	@if [ -x "$(UV)" ] && [ -d dream/.venv ]; then cd dream && $(UV) run ruff check src tests && $(UV) run pytest -q; \
	else echo "dream-test: uv or dream/.venv missing, skipped (make dream-install)"; fi

# Live animation: lance/ui/live.html (served by either replica at /live.html)
# recorded as WebM + GIF by scripts/record-live.py — Playwright from uvx (the
# first run downloads it) drives the system Chrome headless, or the bundled
# Chromium after `uvx --with playwright playwright install chromium`; ffmpeg
# encodes. Output lands in recordings/ (gitignored) unless OUT says otherwise;
# the tour's clip is  make record-live OUT=../docs/structor-tour/images NAME=12-live-jsonl
UVX     ?= $(shell command -v uvx 2>/dev/null || echo $(HOME)/.local/bin/uvx)
URL     ?= http://127.0.0.1:8094/live.html?target=local&mode=replay&minutes=180&speed=60&seconds=18
SECONDS ?= 20
FPS     ?= 4
WIDTH   ?= 1280
HEIGHT  ?= 720
OUT     ?= recordings
NAME    ?= live-jsonl

record-live:
	$(UVX) --with playwright python scripts/record-live.py --url '$(URL)' --seconds $(SECONDS) --fps $(FPS) --width $(WIDTH) --height $(HEIGHT) --out '$(OUT)' --name '$(NAME)'

# launchd owns the local server, both watchers, both Lance replicas, the tray
# and the nightly dream job from login on (templates in launchd/, credentials
# read from ~/.config/structor/*.json by scripts/agent.sh). Stops any
# hand-started copy first so only one instance runs.
install-agents: build lance-install lance-py-install dream-install
	./scripts/install-agents.sh

uninstall-agents:
	./scripts/install-agents.sh --uninstall

agents-status:
	@for a in serve watch-local watch-kvmlab1 lance lance-py tray dream; do \
	  s=$$(launchctl print gui/$$(id -u)/studio.soulbrews.structor.$$a 2>/dev/null | grep -E '^\s(state|pid) =' | tr '\n' ' '); \
	  echo "$$a: $${s:-not loaded}"; done

linux:
	@mkdir -p haos/bin
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GO) build -ldflags "$(LDFLAGS)" -o haos/bin/structor-amd64 .
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags "$(LDFLAGS)" -o haos/bin/structor-aarch64 .
	@ls -la haos/bin

deploy: linux deploy-files

deploy-files:
	./scripts/deploy-haos.sh $(GUEST) $(SLUG)

clean:
	rm -rf bin haos/bin cli/target tray/.build
