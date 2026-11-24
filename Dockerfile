FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /srv/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py ./
COPY data ./data
RUN mkdir -p /data

EXPOSE 5000
# 单 worker 多线程：SQLite 单写者，写事务由 BEGIN IMMEDIATE 串行化
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "run:app"]
