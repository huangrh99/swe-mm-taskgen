FROM visual-harbor-base:cf5dde404aebe150cbb011432e92db366acfd79043f3ca000224bfb976e5466b
RUN rm -rf /app/.git && ln -s /app /testbed
WORKDIR /app
COPY assets /app/assets
RUN git init /app && git -C /app config user.name 'Benchmark Baseline' && git -C /app config user.email 'benchmark@invalid.local' && git -C /app config gc.auto 0 && git -C /app add -A && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' git -C /app commit -q -m 'benchmark baseline' && git -C /app reflog expire --expire=now --all && rm -f /app/.git/gc.pid && git -C /app gc --prune=now --force
WORKDIR /testbed
