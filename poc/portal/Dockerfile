FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
WORKDIR /srv/app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
