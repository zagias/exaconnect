FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY exaconnect_controller ./exaconnect_controller
RUN pip install --no-cache-dir .
USER nobody
EXPOSE 8000
CMD ["uvicorn", "exaconnect_controller.main:app", "--host", "0.0.0.0", "--port", "8000"]
