# 线上（droplet）镜像：财务图表 / Watchlist PE / 内部人交易 / 财报下载。
# 估值报告靠本机 claude CLI，线上用 SEC_DISABLE_VALUATION=1 关掉（见 deploy/docker-compose.yml）。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata：容器 TZ 决定 pe_rank 输出文件名的日期（date.today()），要和本机一致按布里斯班算
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8756
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8756"]
