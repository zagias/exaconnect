# ExaCarib AI phone agent for LiveKit (ADR 0027). Built only with the "voice" profile.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY agent.py .
USER nobody
CMD ["python", "agent.py", "start"]
