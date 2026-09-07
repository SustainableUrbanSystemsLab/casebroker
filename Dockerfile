FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY casebroker ./casebroker
RUN uv pip install --system --no-cache .
# The database is a mounted volume, never baked into the image: the campaign's
# state must outlive any redeploy.
ENV CASEBROKER_DB=/data/campaign.sqlite
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"
CMD ["uvicorn", "casebroker.app:app", "--host", "0.0.0.0", "--port", "8000"]
