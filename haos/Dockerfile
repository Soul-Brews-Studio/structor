# A digest-free default keeps plain local builds working; the HA builder
# overrides BUILD_FROM with the matching per-arch base from build.yaml.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.22
FROM ${BUILD_FROM}

ARG BUILD_VERSION="dev"
ARG BUILD_ARCH="amd64"

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="addon" \
    io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache ca-certificates tzdata

# The binary is cross-compiled by `make linux` (CGO_ENABLED=0, static) and
# copied into this folder by `make deploy` as bin/structor-<arch>.
COPY bin/structor-${BUILD_ARCH} /usr/local/bin/structor
COPY run.sh /run.sh
RUN chmod 0755 /usr/local/bin/structor /run.sh

EXPOSE 8090
CMD ["/run.sh"]
