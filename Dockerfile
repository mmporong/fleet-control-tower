# Build the browser-only 3D view. Its output is copied into the Python server's public /3d/ directory.
FROM node:22-alpine AS web3d-build
WORKDIR /build/web3d
COPY web3d/package.json web3d/package-lock.json ./
RUN npm ci
COPY web3d/index.html web3d/tsconfig.json web3d/vite.config.ts ./
COPY web3d/src/ src/
RUN npm run build

# Control room in one container. The default command runs the simulated DC site + fleet manager + web UI on
# port 8095; `python3 -m fleet.fms` / `fleet.agvsim` run the MQTT runtime from the same image (docs/INTEGRATION.md).
# The Python service has no third-party runtime packages; Node is used only in the frontend build stage above.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY fleet/ fleet/
COPY web/ web/
COPY --from=web3d-build /build/web/3d/ web/3d/
COPY sites/ sites/
# accounts, sessions, the audit log and the runtime's tasks live in SQLite under /data (mount a volume to keep them)
RUN mkdir /data && chown nobody /data
VOLUME /data
USER nobody
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8095/health', timeout=2).status == 200 else 1)"
# the real JD-AMR is only reachable on the host's loopback, so the container runs without it. The first start
# prints a one-time password for the supervisor account admin (or set FCT_ADMIN_PASSWORD).
ENTRYPOINT ["python3", "-m", "fleet.server", "--host", "0.0.0.0", "--no-jdamr", "--db", "/data/server.sqlite3"]
CMD ["--scenario", "standard", "--speed", "5"]
