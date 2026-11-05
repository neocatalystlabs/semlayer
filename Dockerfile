# syntax=docker/dockerfile:1
#
# semlayer as an MCP server, over stdio.
#
#   docker run -i --rm \
#     -e ANTHROPIC_API_KEY \
#     -v "$PWD/layer.yaml:/work/layer.yaml:ro" \
#     semlayer/semlayer mcp /work/layer.yaml
#
# `-i` is not optional: the MCP stdio transport IS the container's stdin and
# stdout, and without it the server reads EOF and exits immediately.
#
# Built from the source tree rather than from the published wheel on purpose,
# so the image matches the commit it was built from. The `warehouses` extra
# is included because a server that cannot reach Snowflake, BigQuery or
# Postgres fails on a stranger's first connection, which is the whole value
# of the image.

FROM python:3.12-slim AS build
WORKDIR /src
# Copied in dependency order: metadata first so a source-only change does not
# invalidate the dependency layer below it.
COPY pyproject.toml README.md LICENSE.md LICENSING.md ./
COPY spec/ ./spec/
COPY src/ ./src/
RUN python -m pip install --no-cache-dir --upgrade pip build \
 && python -m build --wheel --outdir /dist

FROM python:3.12-slim
LABEL org.opencontainers.image.title="semlayer" \
      org.opencontainers.image.description="Infers a semantic layer from your data warehouse and serves it to agents over MCP." \
      org.opencontainers.image.source="https://github.com/semlayer-dev/semlayer" \
      org.opencontainers.image.url="https://semlayer.dev" \
      org.opencontainers.image.licenses="FSL-1.1-ALv2"

# Unprivileged by default. The server reads a layer file and talks to a
# warehouse; it has no reason to be root, and a mounted layer.yaml is read-only.
RUN useradd --create-home --uid 10001 semlayer
COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir "$(ls /tmp/*.whl)[warehouses]" \
 && rm -f /tmp/*.whl
USER semlayer
WORKDIR /work

# ENTRYPOINT is the binary and CMD is the subcommand, so `docker run ... infer`
# or `... --help` work without --entrypoint.
ENTRYPOINT ["semlayer"]
CMD ["mcp"]
