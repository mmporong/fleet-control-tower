# Control room in one container: simulated DC site + fleet manager + web UI on port 8095.
# No third-party packages; the image only needs the Python runtime.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY fleet/ fleet/
COPY web/ web/
USER nobody
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8095/health', timeout=2).status == 200 else 1)"
# the real JD-AMR is only reachable on the host's loopback, so the container runs without it
ENTRYPOINT ["python3", "-m", "fleet.server", "--host", "0.0.0.0", "--no-jdamr"]
CMD ["--scenario", "standard", "--speed", "5"]
