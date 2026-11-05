# =====================================================================
# 证书管理工具（certadmin）镜像 — 构建上下文 = 仓库根目录：
#   docker build -f certadmin/Dockerfile -t vpp-certadmin:1.0.0 .
# 依赖源码：certmgr/（PKI + openssl 封装，工具复用）
#          certadmin/backend/（FastAPI）、certadmin/frontend/（Vue3 单页）
# 与 TB / 设备模拟器解耦，可在独立主机运行（拷贝仓库后构建即可）。
# 证书数据经卷持久化（compose 挂载 certadmin/data -> /data/secrets），务必备份。
# =====================================================================
FROM python:3.12-slim

# certmgr 经 openssl CLI 签发证书，slim 基础镜像需补装 openssl
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY certmgr/certmgr/ certmgr/certmgr/
COPY certadmin/backend/requirements.txt /tmp/ca-requirements.txt
RUN pip install --no-cache-dir -r /tmp/ca-requirements.txt

COPY certadmin/backend/app/ certadmin/backend/app/
COPY certadmin/frontend/ certadmin/frontend/

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /workspace/certadmin/backend
EXPOSE 9001
# CA 私钥目录由 compose 注入 CERTADMIN_SECRETS=/data/secrets
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9001"]
