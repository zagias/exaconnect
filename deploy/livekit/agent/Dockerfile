# ExaCarib AI phone agent for LiveKit (ADR 0033). Built only with the "voice" profile.
FROM python:3.12-slim
WORKDIR /app
ENV HF_HOME=/app/.cache/huggingface
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py runtime.py acceptance.py check_sdk.py configure_sip.py .
RUN python agent.py download-files && chmod -R a+rX /app
USER nobody
CMD ["python", "agent.py", "start"]
