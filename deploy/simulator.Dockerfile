# =====================================================================
# 虚拟电厂物联接入验证项目 — 设备模拟器镜像
# 构建上下文 = 仓库根目录：
#   docker build -f deploy/simulator.Dockerfile -t vpp-simulator:1.0.0 .
# 需要的源码：certmgr/（证书签发 PKI+TB 客户端，模拟器 certapi 复用）
#            simulator/backend/（FastAPI 设备引擎）、simulator/frontend/（Vue3 单页）
# 运行期通过卷挂载 certmgr/secrets（签发/读取设备证书），见 docker-compose.yml
# =====================================================================
FROM python:3.12-slim

# certmgr 经 openssl CLI 签发证书，slim 基础镜像需补装 openssl
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY certmgr/certmgr/ certmgr/certmgr/
COPY simulator/backend/requirements.txt /tmp/sim-requirements.txt
RUN pip install --no-cache-dir -r /tmp/sim-requirements.txt

COPY simulator/backend/app/ simulator/backend/app/
COPY simulator/frontend/ simulator/frontend/

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /workspace/simulator/backend
EXPOSE 9000
# 与宿主机运行方式一致（docs/04 §2）；MQTT/TB 地址由 compose 环境变量注入
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000"]
