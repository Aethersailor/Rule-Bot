# 多阶段构建：编译阶段
FROM python:3.14-alpine AS builder

# 设置构建参数
ARG BUILDKIT_INLINE_CACHE=1

# 安装编译依赖（包括 Rust 编译器）
RUN apk add --no-cache \
    gcc \
    g++ \
    make \
    libffi-dev \
    libsodium-dev \
    musl-dev \
    python3-dev \
    rust \
    cargo \
    openssl-dev \
    pkgconfig

ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
ENV CARGO_BUILD_JOBS=4
ENV OPENSSL_DIR=/usr
ENV OPENSSL_LIBDIR=/usr/lib
ENV PKG_CONFIG_PATH=/usr/lib/pkgconfig
ENV PKG_CONFIG_LIBDIR=/usr/lib/pkgconfig

# 复制依赖文件
COPY requirements.txt .

# Upgrade packaging tools first, then build a deterministic wheelhouse.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# 运行阶段：使用最小化镜像
FROM python:3.14-alpine

# 设置运行时环境变量
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1
ENV TZ=Asia/Shanghai

# 安装运行时依赖（最小化）
RUN apk add --no-cache \
    ca-certificates \
    tzdata \
    && rm -rf /var/cache/apk/* \
    && update-ca-certificates \
    && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

WORKDIR /app

# 从编译阶段复制预编译的wheel包
COPY --from=builder /wheels /wheels

# 安装预编译的包（避免编译）
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# 复制应用代码
COPY src/ ./src/

# 使用非root用户运行（安全考虑）
RUN addgroup -g 1000 appuser && \
    adduser -D -s /bin/sh -u 1000 -G appuser appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["python", "-m", "src.healthcheck"]

CMD ["python", "-m", "src.main"]
