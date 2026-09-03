FROM visual-harbor-base:aacaed53a4cd54d4c052c3d808808d0f15252a655076c584305804586df67ef9
RUN mv /app /testbed \
    && rm -rf /testbed/.git && git init /testbed \
    && git -C /testbed config user.name 'Benchmark Baseline' \
    && git -C /testbed config user.email 'benchmark@invalid.local' \
    && git -C /testbed config gc.auto 0 \
    && git -C /testbed add -A \
    && GIT_AUTHOR_DATE='2000-01-01T00:00:00Z' GIT_COMMITTER_DATE='2000-01-01T00:00:00Z' \
       git -C /testbed commit -q -m 'benchmark baseline' \
    && git -C /testbed reflog expire --expire=now --all \
    && git -C /testbed gc --prune=now
RUN groupadd --gid 10001 benchmark && useradd --uid 10001 --gid 10001 --create-home benchmark \
    && mkdir -p /tests /logs && chmod 0755 /tests \
    && chown -R 10001:10001 /testbed /logs /home/benchmark
WORKDIR /testbed
COPY --chown=10001:10001 assets /testbed/assets
USER 10001:10001
