# Poseidon — production image.
# Build:  docker build -f docker/Dockerfile -t poseidon .
# Run:    see docker/docker-compose.yml

FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# Bundled example algorithms are force-included into the wheel (pyproject.toml)
# so first-run seeding works; the build needs them present in the context.
COPY examples ./examples
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="Poseidon" \
      org.opencontainers.image.description="Autonomous AI trading platform"

# libnotify is intentionally absent in containers; use discord/telegram/email
# notification channels instead of `desktop`.
RUN useradd --create-home --shell /usr/sbin/nologin poseidon
COPY --from=build /install /usr/local
COPY config/poseidon.example.yaml /usr/share/poseidon/poseidon.example.yaml

# /data backs the named volume; pre-create it poseidon-owned so the empty
# named volume inherits writable ownership on first mount.
RUN install -d -o poseidon -g poseidon /data /data/config /data/share

USER poseidon
ENV XDG_CONFIG_HOME=/data/config \
    XDG_DATA_HOME=/data/share \
    POSEIDON_DASHBOARD__HOST=0.0.0.0
VOLUME ["/data"]
EXPOSE 8321

# The dashboard requires a bearer token (0.0.0.0 bind), so the health probe
# authenticates with the same token the container was given.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request,os,sys; \
  f=os.environ.get('POSEIDON_DASHBOARD_TOKEN_FILE'); \
  t=(open(f).read().strip() if f else os.environ.get('POSEIDON_DASHBOARD_TOKEN','')); \
  h={'Authorization':'Bearer '+t} if t else {}; \
  r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8321/api/status',headers=h),timeout=5); \
  sys.exit(0 if r.status==200 else 1)"

ENTRYPOINT ["poseidon"]
CMD ["run"]
