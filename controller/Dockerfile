FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY exaconnect_controller ./exaconnect_controller
RUN pip install --no-cache-dir . && mkdir -p /data && chown nobody:nogroup /data
USER nobody
ENV EXA_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "exaconnect_controller.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
