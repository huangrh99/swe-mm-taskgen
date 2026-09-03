FROM node@sha256:8a34c4ab3ea2c5cd194f07e317b2a8f09461d3c8b05c4e34c8ccd56d56024c4d AS frozen-agent-node
FROM visual-harbor-base:028af52db0d7fc592c95c5ea04d310e4b731ff62e22ca28967ec25c977499511

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

COPY bin/rg /usr/local/bin/rg
RUN chmod 0755 /usr/local/bin/rg && rg --version >/dev/null
RUN rm -rf /app/.git \
    && ln -sfn /app /testbed \
    && git init /app \
    && git -C /app config user.name 'Benchmark Baseline' \
    && git -C /app config user.email 'benchmark@invalid.local' \
    && git -C /app config gc.auto 0 \
    && git -C /app add -A \
    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' \
       git -C /app commit -q -m 'benchmark baseline' \
    && git -C /app reflog expire --expire=now --all \
    && rm -f /app/.git/gc.pid \
    && git -C /app gc --prune=now --force
COPY assets /app/assets
RUN command -v bash \
    && command -v curl \
    && command -v node \
    && command -v npm \
    && command -v rg \
    && bash --version >/dev/null \
    && curl --version >/dev/null \
    && node --version \
    && npm --version \
    && rg --version >/dev/null
WORKDIR /testbed
