FROM node@sha256:8a34c4ab3ea2c5cd194f07e317b2a8f09461d3c8b05c4e34c8ccd56d56024c4d AS frozen-agent-node
FROM visual-harbor-base:b5f88c50a793667139a1721b5545ee951d7f918992f515c90823c2c6b5befd98

COPY --from=frozen-agent-node /usr/local/ /opt/frozen-agent-node/
# Harbor 0.22 KimiCode always runs npm install. Freeze both CLIs and npm cache
# at build time; the fail-closed apk marker selects its offline Node branch.
RUN mkdir -p /root/.local /root/.nvm \
    && export PATH="/opt/frozen-agent-node/bin:${PATH}" \
    && npm install --global --prefix /root/.local @openai/codex@0.148.0 @moonshot-ai/kimi-code@0.29.0 \
    && npm cache verify \
    && npm config set offline true --location=user \
    && printf '%s\n' 'export PATH="/opt/frozen-agent-node/bin:/root/.local/bin:$PATH"' > /root/.nvm/nvm.sh \
    && printf '%s\n' '#!/bin/sh' 'echo "apk is disabled in this frozen glibc image" >&2' 'exit 1' > /usr/local/bin/apk \
    && chmod 0755 /usr/local/bin/apk
ENV PATH="/root/.local/bin:${PATH}"
RUN . /root/.nvm/nvm.sh && command -v kimi && kimi --version | grep -Fx '0.29.0' \
    && command -v codex && codex --version | grep -Fx 'codex-cli 0.148.0'

# Harbor's Codex agent requires ripgrep at runtime. The pinned aarch64 binary
# is task material, so image construction and agent setup never contact apt.
COPY bin/rg /usr/local/bin/rg
RUN chmod 0755 /usr/local/bin/rg && rg --version >/dev/null

# The checked-out Carbon monorepo resolves workspace packages from their built
# entry points. Build @carbon/react and the complete dependency graph offline,
# then fail the image build if the two dependencies required by this test were
# not emitted. This prevents a missing workspace build from being misreported
# as a semantic test failure.
RUN yarn lerna run build --scope @carbon/react --include-dependencies --stream --prefix \
    && test -f /app/packages/feature-flags/lib/index.js \
    && test -f /app/packages/icons-react/lib/index.js

RUN rm -rf /app/.git && ln -s /app /testbed
WORKDIR /app
COPY assets /app/assets
RUN git init /app \
    && git -C /app config user.name 'Benchmark Baseline' \
    && git -C /app config user.email 'benchmark@invalid.local' \
    && git -C /app config gc.auto 0 \
    && git -C /app add -A \
    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' git -C /app commit -q -m 'benchmark baseline' \
    && git -C /app reflog expire --expire=now --all \
    && rm -f /app/.git/gc.pid \
    && git -C /app gc --prune=now --force
WORKDIR /testbed
