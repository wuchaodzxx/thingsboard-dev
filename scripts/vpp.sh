#!/usr/bin/env bash
# =====================================================================
# 虚拟电厂物联接入验证环境 — 一键脚本（macOS / Linux）
# 用法：scripts/vpp.sh <命令> [参数]
#   init      初始化：检查依赖 → 生成 deploy/.env → 创建 .venv 并安装依赖
#   start     启动 TB（compose up -d，等待 healthy）+ 模拟器（宿主机 uvicorn :19000）
#   stop      停止模拟器 + TB（保留容器与数据卷）
#   restart   重启（= stop + start）
#   status    组件状态总览（TB / 模拟器 / 端口 / 测试产物）
#   logs      日志跟踪：logs [tb|sim]（默认 tb）
#   test      自动化测试：test [规模]（默认 1 全量；>1 仅并发用例组）
#   backup    备份 TB 数据卷（自动停/启 TB 保证一致性）到 backups/
# 端口/镜像等在 deploy/.env 配置（模板 deploy/.env.example）。
# =====================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$ROOT/deploy"
ENV_FILE="$DEPLOY/.env"
SIM_LOG="$ROOT/tests/sim_uvicorn.log"
SIM_PID="$ROOT/tests/sim_uvicorn.pid"
SIM_PORT="${SIM_HTTP_PORT:-19000}"
TB_PORT="${TB_HTTP_PORT:-18080}"
MQTTS_PORT="${MQTTS_PORT:-18885}"
# deploy/.env 中同名变量优先（脚本环境未导出时逐行解析）
if [ -f "$ENV_FILE" ]; then
  for _k in SIM_HTTP_PORT TB_HTTP_PORT MQTTS_PORT; do
    _v="$(grep -E "^${_k}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2 || true)"
    [ -n "${_v:-}" ] && eval "${_k}=${_v}"
  done
fi

say() { printf '%s\n' "$*"; }
py() {
  if [ -x "$ROOT/.venv/bin/python" ]; then echo "$ROOT/.venv/bin/python"
  elif [ -x "$ROOT/.venv/Scripts/python.exe" ]; then echo "$ROOT/.venv/Scripts/python.exe"
  else command -v python3; fi
}
compose() {
  if [ -f "$ENV_FILE" ]; then
    docker compose --project-directory "$DEPLOY" --env-file "$ENV_FILE" -f "$DEPLOY/docker-compose.yml" "$@"
  else
    docker compose --project-directory "$DEPLOY" -f "$DEPLOY/docker-compose.yml" "$@"
  fi
}
port_pid() {  # 监听指定 TCP 端口的进程 PID（无则输出空）
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1 || true
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk -v s=":$1 " '$4 ~ s {print}' | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true
  fi
}
wait_http() {  # wait_http <url> <超时秒> <就绪日志提示>
  local url="$1" timeout="$2" msg="$3" i=0
  until curl -sf -o /dev/null "$url"; do
    i=$((i + 2)); [ "$i" -ge "$timeout" ] && { say "✗ 超时（${timeout}s）：$msg"; return 1; }
    sleep 2
  done
  say "✓ $msg"
}

cmd_init() {
  say "== 初始化 =="
  command -v docker >/dev/null || { say "✗ 未找到 docker"; exit 1; }
  docker compose version >/dev/null 2>&1 || { say "✗ 未找到 Docker Compose v2"; exit 1; }
  command -v openssl >/dev/null || say "⚠ 未找到 openssl（certmgr 签发证书需要）"
  if [ ! -f "$ENV_FILE" ]; then cp "$DEPLOY/.env.example" "$ENV_FILE"; say "✓ 已生成 deploy/.env（可按需修改端口/JVM）"; else say "· deploy/.env 已存在，跳过"; fi
  if [ ! -x "$(py)" ]; then say "✗ 未找到 Python 3（需 3.11+）"; exit 1; fi
  if [ ! -d "$ROOT/.venv" ]; then
    (cd "$ROOT" && python3 -m venv .venv) && say "✓ 已创建 .venv"
  else say "· .venv 已存在，跳过创建"; fi
  (cd "$ROOT" && "$(py)" -m pip install -q --upgrade pip \
    && "$(py)" -m pip install -q -r certmgr/requirements.txt -r simulator/backend/requirements.txt \
       pytest jsonschema) && say "✓ 依赖安装完成（certmgr + 模拟器 + pytest/jsonschema）"
  say "下一步：scripts/vpp.sh start"
}

sim_start() {
  local pid; pid="$(port_pid "$SIM_PORT" || true)"
  if [ -n "$pid" ]; then say "· 端口 ${SIM_PORT} 已被监听（pid=${pid}），跳过模拟器启动"; return 0; fi
  say "· 启动模拟器（uvicorn :${SIM_PORT}，日志 ${SIM_LOG}）…"
  pushd "$ROOT" >/dev/null
  nohup "$(py)" -m uvicorn app.main:app --host 0.0.0.0 --port "$SIM_PORT" \
    --app-dir "$ROOT/simulator/backend" >> "$SIM_LOG" 2>&1 < /dev/null &
  local simp=$!
  popd >/dev/null
  echo "$simp" > "$SIM_PID"
  # 脱离作业表：脚本退出不再 wait4 后台任务，uvicorn 由 nohup 保护独立运行
  disown "$simp" 2>/dev/null || true
  wait_http "http://localhost:$SIM_PORT/api/health" 60 "模拟器就绪 http://localhost:$SIM_PORT/"
}

sim_stop() {
  local pid; pid="$(port_pid "$SIM_PORT" || true)"
  [ -z "$pid" ] && [ -f "$SIM_PID" ] && pid="$(cat "$SIM_PID" 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null || true
    say "✓ 模拟器已停止（pid=${pid}）"
  else say "· 模拟器未在运行"; fi
  rm -f "$SIM_PID"
}

cmd_start() {
  say "== 启动 TB =="
  compose up -d
  wait_http "http://localhost:$TB_PORT/login" 300 "TB 就绪 http://localhost:$TB_PORT/（首次启动建库约 1~3 分钟）"
  say "== 启动模拟器 =="
  sim_start
  cmd_status
}

cmd_stop() {
  say "== 停止 =="
  sim_stop
  compose stop
  say "✓ TB 已停止（数据卷保留；彻底重置用 docker compose down -v，慎用）"
}

cmd_restart() { cmd_stop; cmd_start; }

cmd_status() {
  say "== 组件状态 =="
  local tb_state; tb_state="$(docker inspect -f '{{.State.Status}}' vpp-thingsboard 2>/dev/null || echo '未运行')"
  say "TB 容器    : $tb_state"
  local http; http="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$TB_PORT/login" 2>/dev/null || echo '000')"
  say "TB HTTP    : http://localhost:$TB_PORT/login -> $http"
  local tls; tls="未监听"
  if command -v nc >/dev/null 2>&1 && nc -z localhost "$MQTTS_PORT" >/dev/null 2>&1; then tls="监听中(:$MQTTS_PORT)"; fi
  say "MQTT TLS   : $tls"
  local sim; sim="$(curl -sf "http://localhost:$SIM_PORT/api/health" 2>/dev/null || true)"
  if [ -n "$sim" ]; then
    say "模拟器     : http://localhost:$SIM_PORT/ 在线 $(echo "$sim" | head -c 160)…"
  else say "模拟器     : 离线"; fi
  say "-- 测试产物 --"
  local f
  for f in tests/results_p6_s1.json tests/results_p6_s10.json tests/results_p6_s100.json tests/report_p6.html; do
    [ -f "$ROOT/$f" ] && say "  ✓ ${f}" || say "  - ${f}（无）"
  done
}

cmd_logs() {
  case "${1:-tb}" in
    tb)  compose logs -f --tail=100 ;;
    sim) tail -n 100 -f "$SIM_LOG" ;;
    *)   say "用法：scripts/vpp.sh logs [tb|sim]"; exit 1 ;;
  esac
}

cmd_test() {
  local scale="${1:-1}"
  say "== 自动化测试（scale=${scale}）=="
  cd "$ROOT"
  if [ "$scale" -gt 1 ]; then
    "$(py)" -m pytest tests/p6 --scale "$scale" -m scale -v
  else
    "$(py)" -m pytest tests/p6 --scale "$scale" -v
  fi
}

cmd_backup() {
  say "== 备份 TB 数据卷（先停 TB 保证一致性，完成后自动启动）=="
  local out="$ROOT/backups"; mkdir -p "$out"
  local name="vpp-tb-data-$(date +%Y%m%d-%H%M%S).tgz"
  local was_running; was_running="$(docker inspect -f '{{.State.Status}}' vpp-thingsboard 2>/dev/null || echo '未运行')"
  compose stop
  docker run --rm -v vpp-tb-data:/src:ro -v "$out":/bak alpine \
    sh -c "cd /src && tar czf /bak/$name ."
  if [ "$was_running" = "running" ]; then compose start; wait_http "http://localhost:$TB_PORT/login" 300 "TB 已恢复"; fi
  say "✓ 备份完成：backups/$name"
}

case "${1:-status}" in
  init)    cmd_init ;;
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    shift; cmd_logs "${1:-tb}" ;;
  test)    shift; cmd_test "${1:-1}" ;;
  backup)  cmd_backup ;;
  *) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
