# StockInvestmentTool — 生产镜像
# 构建上下文 = 本目录（StockInvestmentTool/），运行数据由 .dockerignore 排除
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 时区 + matplotlib/pandas 所需系统库（数据源按国内交易时间）
# apt 先换清华 Debian 源，避免国内服务器访问 deb.debian.org 超时
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends tzdata libgomp1 fontconfig \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 依赖（内置国内 pip 源 + buildkit 缓存挂载，云端构建快；PIP_INDEX_URL 可覆盖）
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN --mount=type=cache,target=/pip-cache \
    pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# 应用代码（.dockerignore 已排除 output/*.db/docs 等）
COPY . /app/StockInvestmentTool/

# 运行数据（portfolio.db / 缓存 / 报表）持久化挂载
VOLUME /app/StockInvestmentTool/output

EXPOSE 9000

# waitress 生产 WSGI；create_app() 内会启动 APScheduler 每日任务
CMD ["waitress-serve", "--call", "--host=0.0.0.0", "--port=9000", "StockInvestmentTool.web.app:create_app"]
