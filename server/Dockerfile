FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV MEDPLAT_SEED_DEMO=1
EXPOSE 8000
CMD ["sh", "start.sh"]
