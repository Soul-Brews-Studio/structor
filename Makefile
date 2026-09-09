# Structor — build / test / run / deploy
#
#   make build        Go server (bin/structor) + Rust CLI (bin/structor-cli)
#   make test         go test + cargo test (+ swift build of the tray)
#   make run          serve on 127.0.0.1:8090 with a local superuser
#   make scan         one CLI pass over ~/.claude/projects into the local server
#   make watch        follow ~/.claude/projects
#   make tray         build + launch the macOS menu-bar app
#   make linux        static linux/amd64 + linux/arm64 server binaries for HAOS
#   make deploy       rsync the add-on to kvmlab1:/addons/structor and (re)install it
#
# Go and Rust toolchains live outside PATH on this machine; override if yours differ.
GO      ?= $(HOME)/sdk/go/bin/go
CARGO   ?= $(HOME)/.rustup/toolchains/stable-aarch64-apple-darwin/bin/cargo
SWIFT   ?= swift
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

.PHONY: build build-go build-cli test test-go test-cli test-tray run scan watch status tray tray-app install-tray install-agents uninstall-agents agents-status linux deploy deploy-files clean

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

test: test-go test-cli test-tray

test-go:
	$(GO) vet ./... && $(GO) test ./...

test-cli:
	cd cli && PATH="$(dir $(CARGO)):$$PATH" $(CARGO) test

test-tray:
	cd tray && $(SWIFT) build 2>&1 | tail -3

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

# launchd owns the local server, both watchers and the tray from login on
# (templates in launchd/, credentials read from ~/.config/structor/*.json by
# scripts/agent.sh). Stops any hand-started copy first so only one instance runs.
install-agents: build
	./scripts/install-agents.sh

uninstall-agents:
	./scripts/install-agents.sh --uninstall

agents-status:
	@for a in serve watch-local watch-kvmlab1 tray; do \
	  launchctl print gui/$$(id -u)/studio.soulbrews.structor.$$a 2>/dev/null | grep -E '^\s(state|pid) =' | tr '\n' ' ' | sed "s|^|$$a: |"; echo; done

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
