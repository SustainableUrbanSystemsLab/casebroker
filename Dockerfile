FROM python:3.12-slim
# libexpat1: the rasterio wheel bundles GDAL, and GDAL links against the
# system libexpat, which the slim base image does not ship. Without it the
# package installs cleanly and then fails at import with
# "libexpat.so.1: cannot open shared object file" -- so terrain and canopy
# came back "unavailable" for every case in production while the wheel sat
# there installed, and the unit suite (run on a dev machine that has the
# library) stayed green. The Docker CI job now imports rasterio and performs a
# real /vsicurl read so this cannot regress silently.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libexpat1 \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY casebroker ./casebroker
RUN uv pip install --system --no-cache .
# NO default CASEBROKER_DB, deliberately. This image previously shipped
# `/data/campaign.sqlite` with a VOLUME, on the reasoning that a mounted volume
# outlives a redeploy -- true on a VM, false on the platform this actually runs
# on. Render's service has no persistent disk, so that default wrote the whole
# campaign to a container filesystem that is discarded on every deploy, and
# nothing said so. Production points CASEBROKER_DB at Supabase Postgres; with
# state external the service needs no disk at all. app.py warns loudly on
# startup if this resolves to SQLite.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["uvicorn", "casebroker.app:app", "--host", "0.0.0.0", "--port", "8000"]
