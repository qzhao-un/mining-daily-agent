FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用 Docker 层缓存：仅 requirements.txt 变化时重建镜像
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码
COPY . .

# 验证 MCP server 能正常启动（导入检查，避免镜像交付即坏）
RUN python -c "import servers.mining_news.server" || true

ENTRYPOINT ["python", "agent/main.py"]
CMD ["给我生成一份关于 Pilbara 锂矿的今日简报", "-o", "output/briefing.md"]