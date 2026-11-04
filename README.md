# 虚拟电厂物联接入验证环境

基于 **ThingsBoard CE 3.5.1**（固定镜像 `thingsboard/tb-postgres:3.5.1`）构建的虚拟电厂（VPP）物联接入验证项目：MQTT over TLS（X.509 双向认证）设备接入（pv / pcs / load / wind 四类设备）、设备模拟器与验证控制台、36 例自动化测试（1/10/100 三档并发）、交付级技术规范与双平台一键脚本。v1.2 迭代新增：负荷（load，原 flex）与风电（wind）设备类型、负荷特征模拟（基线/波动/占空启停/风速）、手动配置证书+手动注册、日志数据方向与原始报文记录。

## 快速上手（macOS / Linux）

```bash
# 1. 初始化：依赖检查 → 生成 deploy/.env → 创建 .venv 并安装依赖
scripts/vpp.sh init

# 2. 启动：TB（compose，首次建库约 1~3 分钟）+ 模拟器（uvicorn :19000）
scripts/vpp.sh start

# 3. 自动化测试：规模 1 全量 30 例（需 init+start 完成）；10/100 仅并发用例组
scripts/vpp.sh test        # 等价 test 1
scripts/vpp.sh test 10
scripts/vpp.sh test 100
```

启动后入口：

| 入口 | 地址 | 说明 |
|---|---|---|
| 验证控制台（模拟器前端） | http://localhost:19000/ | 设备管理/异常注入/证书管理/报文日志 |
| ThingsBoard Web UI | http://localhost:18080/ | 演示账号 `tenant@thingsboard.org` / `tenant` |
| MQTT over TLS（主接入） | localhost:18885 | X.509 双向认证（证书经控制台或 certmgr 签发） |

## 常用命令

| 命令 | 行为 |
|---|---|
| `scripts/vpp.sh status` | 组件状态总览（TB/模拟器/端口/测试产物） |
| `scripts/vpp.sh stop` / `restart` | 停止（保留数据卷）/ 重启 |
| `scripts/vpp.sh logs [tb\|sim]` | 跟踪 TB 或模拟器日志 |
| `scripts/vpp.sh backup` | 备份 TB 数据卷到 `backups/vpp-tb-data-<时间戳>.tgz`（自动停/启 TB） |

Windows：使用 `scripts/vpp.ps1`，子命令与参数完全一致（`powershell -File scripts\vpp.ps1 start`；跨平台 PowerShell 7/pwsh 亦可运行）。

## 目录结构

```
thingsboard-dev/
├── README.md                        # 本文件
├── docs/                            # 00 总体规划 / 01 报文规范 / 02 架构部署 / 03 证书手册
│   │                                # 04 模拟器使用 / 05 测试方案与实测 / 技术规范终稿
├── spec/schemas/                    # 机器可读 JSON Schema（15 个，与 docs/01 同步）
├── deploy/                          # docker-compose + .env.example + simulator.Dockerfile
├── certmgr/                         # 证书签发/管理（secrets 不入库）
├── simulator/                       # 设备模拟器后端（FastAPI）+ 前端（Vue3）
├── tests/                           # P6 pytest 用例 + results_p6_s*.json + report_p6.html
├── scripts/                         # vpp.sh / vpp.ps1 一键脚本
└── backups/                         # TB 数据卷备份（vpp.sh backup 产物）
```

## 设备模拟器的两种运行方式

- **宿主机进程**（默认，`vpp.sh start` 采用）：`.venv` 内 uvicorn :19000，日志 `tests/sim_uvicorn.log`；
- **容器化**（可选）：模拟器并入 compose（profile `simulator`，默认不启动）：

```bash
docker compose --project-directory deploy --env-file deploy/.env \
  --profile simulator up -d --build     # 镜像 vpp-simulator:1.0.0，端口 19000:9000
```

容器内经网络别名 `tb:8883` 接入（服务器证书 SAN 含 `DNS:tb`），TB 地址与 MQTT 端口由编排注入，证书目录只读挂载。

## 端口分配（对外统一 1xxxx）

| 对外端口 | 内部 | 用途 |
|---|---|---|
| 18080 | 9090 | TB Web / REST API |
| 18885 | 8883 | MQTT over TLS（主接入，X.509 双向认证） |
| 18886 | 1883 | MQTT 明文（仅调试，默认关闭） |
| 19000 | 9000 | 模拟器 / 验证控制台 |

端口冲突时修改 `deploy/.env`（模板 `deploy/.env.example`）后重新 `start`。

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/00-项目总体规划.md](docs/00-项目总体规划.md) | 目标、选型、阶段门禁、变更记录 |
| [docs/01-物联交互报文规范.md](docs/01-物联交互报文规范.md) | M01~M16 报文详规、示例、时序图全集 |
| [docs/02-系统架构与部署手册.md](docs/02-系统架构与部署手册.md) | 部署实测记录、运维命令、常见问题 |
| [docs/03-证书管理操作手册.md](docs/03-证书管理操作手册.md) | PKI 体系、certmgr 命令、失败用例实测 |
| [docs/04-模拟器使用说明.md](docs/04-模拟器使用说明.md) | 控制台用法、故障注入、联调实测 |
| [docs/05-自动化测试方案与用例.md](docs/05-自动化测试方案与用例.md) | 30 例用例、1/10/100 实测结果 |
| [docs/虚拟电厂物联设备通信与接入技术规范.md](docs/虚拟电厂物联设备通信与接入技术规范.md) | **交付终稿**：协议+安全+性能指标汇编 |

## 平台行为要点（设备厂家必读，实测确认）

- TB 3.5.1 MQTT TLS 握手层为 WANT 模式：设备认证在 MQTT CONNECT 层按证书指纹强制，未注册指纹 CONNACK rc≠0，过期证书 rc=5；
- 设备凭据以 base64(DER) 存储；设备 `active` 每 60s 周期判定（profile `inactivityTimeout`，可用设备级属性覆盖）；
- 对外端口为 **18885**（原规划 18883 因占用已调整，全部文档已同步）。

完整规范见技术规范终稿 §3.4/§3.5。
