"""设备模拟引擎：DeviceSim（X.509 接入、周期遥测、心跳、断线缓存补传、
RPC 指令闭环、共享属性回执、异常注入）与 DeviceManager。

对齐 docs/01-物联交互报文规范 v1.0（M01~M14）与 spec/schemas/*.json。
"""
from __future__ import annotations

import copy
import json
import logging
import queue
import random
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass

import paho.mqtt.client as mqtt

from . import curves
from .logs import LOGS

log = logging.getLogger("sim.devices")

T_TELEMETRY = "v1/devices/me/telemetry"
T_ATTRIBUTES = "v1/devices/me/attributes"          # 双向：设备上报客户端属性 / 接收共享属性
T_RPC_REQ = "v1/devices/me/rpc/request/+"          # 订阅
CACHE_MAX = 1536                                    # ≥24h：1440×1min + 96×15min
QUERY_CMDS = {"readRealtimeData", "readHistoryData", "getDeviceParams"}
CTRL_CMDS = {"setDeviceParams", "setActivePowerTarget", "setMaxPowerLimit",
             "setRunMode", "setChargeDischargePower"}
SETTABLE_PARAMS = {"rampRate", "qMin", "qMax", "respTimeS", "regMode", "upTimeS"}


@dataclass
class DeviceConfig:
    name: str
    devType: str = "pv"                # pv | pcs | flex
    capKw: float = 100.0
    devModel: str = "SIM-100"
    vendor: str = "VPP-Lab"
    swVer: str = "V1.0.0"
    hwVer: str = "H1.0"
    # M07 约束
    pMin: float = 0.0
    pMax: float = 100.0
    rampRate: float = 50.0             # kW/min
    qMin: float = -30.0
    qMax: float = 30.0
    socMin: float = 10.0
    socMax: float = 95.0
    chgPMax: float = 100.0
    disPMax: float = 100.0
    respTimeS: int = 5
    ctrlMode: int = 1
    regMode: str = "auto"
    upTimeS: int = -1
    # M08 共享配置（当前应用值）
    hbPeriodSec: int = 30
    tele1mEnable: bool = True
    tele15mEnable: bool = True
    cfgVer: int = 0                    # 已应用的配置版本（0=默认）

    def devinfo(self) -> dict:         # M01
        return {"devSn": f"SN{self.name.upper()}", "devName": self.name,
                "devType": self.devType, "devModel": self.devModel,
                "vendor": self.vendor, "swVer": self.swVer, "hwVer": self.hwVer,
                "protocolVer": "vpp-1.0", "capKw": self.capKw,
                "firstBootTs": int(time.time() * 1000)}

    def constraints(self) -> dict:     # M07
        d = {k: getattr(self, k) for k in
             ("pMin", "pMax", "rampRate", "qMin", "qMax", "respTimeS", "ctrlMode", "regMode", "upTimeS")}
        if self.devType == "pcs":
            d.update({"socMin": self.socMin, "socMax": self.socMax,
                      "chgPMax": self.chgPMax, "disPMax": self.disPMax})
        return d

    @staticmethod
    def defaults_for(dev_type: str, name: str, cap_kw: float) -> "DeviceConfig":
        c = DeviceConfig(name=name, devType=dev_type, capKw=cap_kw)
        c.pMin, c.pMax = (0.0, cap_kw) if dev_type == "pv" else ((-cap_kw, cap_kw) if dev_type == "pcs" else (-cap_kw, 0.0))
        c.rampRate = max(10.0, cap_kw / 2)
        if dev_type == "pcs":
            c.chgPMax = c.disPMax = cap_kw
        return c


class DeviceSim:
    """单设备模拟实例：独立 MQTT 连接与状态机，故障互不影响。"""

    def __init__(self, cfg: DeviceConfig, *, mqtt_host: str, mqtt_port: int,
                 ca_file: str, cert_file: str, key_file: str):
        self.cfg = cfg
        self.mqtt_host, self.mqtt_port = mqtt_host, mqtt_port
        self.ca_file, self.cert_file, self.key_file = ca_file, cert_file, key_file

        # 运行状态
        self.connected = False
        self._want_connect = False
        self._user_disconnected = False
        self._retry_attempt = 0
        self._next_retry_ts = 0.0
        self._drop_streak = 0          # 连续异常断链计数（毒报文防御）
        self._last_online_ts = 0.0
        self.started_at = int(time.time() * 1000)
        self.last_error: str | None = None
        self.last_worker_err: str | None = None
        self.hb_seq = 0
        self.last_hb_ts = 0.0

        # 功率/电量状态
        self.cur_p = 0.0
        self.soc = 60.0 if cfg.devType == "pcs" else None
        self.active_target: float | None = None       # setActivePowerTarget 目标
        self._target_until: float | None = None       # durationS 到期回退
        self.p_limit: dict[str, float | None] = {"gen": None, "chg": None, "dis": None}
        self.stopped = False
        self.q = round(random.uniform(-5, 5), 2)
        self.cyc_cnt = 100
        self.shed_state = 0
        seed = f"{cfg.name}:{cfg.devType}"
        self._e_imp = curves.wrap_meter(10000.0 + random.uniform(0, 100))
        self._e_exp = curves.wrap_meter(5000.0 + random.uniform(0, 100))
        self._base_imp: float | None = None           # 上周期末表码（15min 基线）
        self._base_exp: float | None = None
        self._e_day = 0.0
        self._e_day_idx = curves.day_index_utc8(int(time.time() * 1000))

        # 断线缓存与补传
        self.cache: deque[dict] = deque(maxlen=CACHE_MAX)
        self.cache_overflow = 0
        self._replaying = False
        self._next_batch_ts = 0.0
        self.replayed_batches = 0

        # 指令台账与执行
        self.ledger: dict[str, dict] = {}             # cmdId → {state, errCode, data?, ts}
        self.executing: dict | None = None            # {cmdId, cmdType, startedMs}
        self._exec_thread: threading.Thread | None = None
        self._target_timer: threading.Timer | None = None

        # 异常注入（每设备独立）
        self.fault: dict = {"disconnectUntil": 0.0, "delayMs": 0, "delayUntil": 0.0,
                            "dupRemaining": 0, "badJsonRemaining": 0,
                            "missingRemaining": 0, "missingField": "p",
                            "oorRemaining": 0, "oorKey": "p", "oorValue": 99999.0,
                            "rejectRemaining": 0, "failRemaining": 0}

        self.last_1m_end = 0
        self.last_15m_end = 0
        self.sent_count = 0
        self._stop_flag = False
        # 出站队列：paho publish() 直写 socket，跨线程并发写同一 SSL 对象会段错误
        # （paho 1.6.1 未做发送线程收敛），故所有报文先入队，由 worker 线程统一 flush
        self._tx_queue: queue.Queue = queue.Queue()
        self._pending_disconnect = False

        self.cli = mqtt.Client(client_id=f"sim-{cfg.name}", protocol=mqtt.MQTTv311)
        self.cli.on_connect = self._on_connect
        self.cli.on_disconnect = self._on_disconnect
        self.cli.on_message = self._on_message
        self.cli.tls_set(ca_certs=self.ca_file, certfile=self.cert_file, keyfile=self.key_file,
                         cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        # 自管网络循环：由 _worker 每 tick 调 cli.loop(0) 驱动收发，
        # 不使用 paho loop_start 线程（其主动断开后线程死亡且与手动重连形成双轨竞争）
        self._thread = threading.Thread(target=self._worker, name=f"sim-{cfg.name}", daemon=True)
        self._thread.start()

    # ---------------- 生命周期 ----------------
    def start_connect(self):
        self._user_disconnected = False
        self._want_connect = True
        self._next_retry_ts = 0.0

    def disconnect(self, reason: str = "manual"):
        self._user_disconnected = True
        self._want_connect = False
        if self.connected:
            self._pending_disconnect = True  # 由 worker 线程执行断开（socket 写单线程化）
            LOGS.add(device=self.cfg.name, direction="evt", result="offline", error=reason)

    def _hard_close(self):
        """flush 队列中的 DISCONNECT 包并确保底层 socket 关闭。

        paho 1.6.1 的 disconnect() 仅入队 DISCONNECT 包并置状态，socket 的
        关闭发生在 _packet_write 写完该包之后；若线程随即退出或不再调 loop，
        包滞留、socket 泄漏，服务端会话不注销（TB active 恒为 true）。
        必须用 paho 自己的 _sock_close() 清空其内部 _sock 引用：若仅裸 close
        底层 socket，paho 仍持已关闭的 SSL 对象，下次 loop() 在 _sock.pending()
        处持续抛异常（该调用位于 select 异常保护之外），设备将永久静默。"""
        try:
            for _ in range(6):               # flush DISCONNECT（写完即关 socket）
                self.cli.loop(0.05)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.cli._sock_close()           # 关闭并清空 paho 内部引用
        except Exception:  # noqa: BLE001
            pass
        try:
            s = self.cli.socket()
            if s is not None:
                s.close()                    # 兜底：任何残留 socket 一律关闭
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self):
        self._stop_flag = True
        self._want_connect = False
        self._pending_disconnect = True
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)  # 等 worker 结束最后的网络收发
        if self.connected:
            self.cli.disconnect()
            self._hard_close()

    def status(self) -> dict:
        return {
            "name": self.cfg.name, "devType": self.cfg.devType, "capKw": self.cfg.capKw,
            "connected": self.connected, "wantConnect": self._want_connect,
            "userDisconnected": self._user_disconnected,
            "retryAttempt": self._retry_attempt, "lastError": self.last_error,
            "uptimeSec": (int(time.time() * 1000) - self.started_at) // 1000,
            "hbSeq": self.hb_seq, "curP": self.cur_p, "soc": self.soc,
            "runState": self._run_state(), "activeTarget": self.active_target,
            "pLimit": self.p_limit, "stopped": self.stopped,
            "cfgVer": self.cfg.cfgVer, "hbPeriodSec": self.cfg.hbPeriodSec,
            "cacheCount": len(self.cache), "cacheOverflow": self.cache_overflow,
            "replaying": self._replaying, "replayedBatches": self.replayed_batches,
            "sentCount": self.sent_count, "executing": self.executing,
            "ledgerSize": len(self.ledger), "fault": self.active_faults(),
        }

    def active_faults(self) -> dict:
        f, now = self.fault, time.time()
        act: dict = {}
        if f["disconnectUntil"] > now:
            act["disconnect"] = int(f["disconnectUntil"] - now)
        if f["delayUntil"] > now:
            act["delayMs"] = f["delayMs"]
        for k in ("dup", "badJson", "missing", "oor", "reject", "fail"):
            if f[f"{k}Remaining"] > 0:
                act[k] = f[f"{k}Remaining"]
        return act

    def inject(self, mode: str, *, duration_sec: float = 0, count: int = 0,
               ms: int = 300, key: str = "p", value: float = 99999.0,
               field: str = "p") -> dict:
        f = self.fault
        if mode == "disconnect":
            f["disconnectUntil"] = time.time() + duration_sec
        elif mode == "delay":
            f["delayMs"], f["delayUntil"] = ms, time.time() + duration_sec
        elif mode == "duplicate":
            f["dupRemaining"] = count
        elif mode == "badJson":
            f["badJsonRemaining"] = count
        elif mode == "missingField":
            f["missingRemaining"], f["missingField"] = count, field
        elif mode == "outOfRange":
            f["oorRemaining"], f["oorKey"], f["oorValue"] = count, key, value
        elif mode == "rejectCmd":
            f["rejectRemaining"] = count
        elif mode == "failCmd":
            f["failRemaining"] = count
        else:
            raise ValueError(f"未知注入类型 {mode}")
        LOGS.add(device=self.cfg.name, direction="evt", msg_type="inject",
                 result="ok", error=json.dumps({"mode": mode, "durationSec": duration_sec,
                                                "count": count, "ms": ms}, ensure_ascii=False))
        return self.active_faults()

    # ---------------- MQTT 回调 ----------------
    def _on_connect(self, _c, _u, _f, rc, _p=None):
        if rc != 0:
            self.last_error = f"CONNACK rc={rc}"
            LOGS.add(device=self.cfg.name, direction="evt", result="error", error=self.last_error)
            return
        self.connected = True
        self._retry_attempt = 0
        self._last_online_ts = time.time()
        self.last_error = None
        self.cli.subscribe(T_ATTRIBUTES, qos=1)
        self.cli.subscribe(T_RPC_REQ, qos=1)
        LOGS.add(device=self.cfg.name, direction="evt", topic=T_RPC_REQ, result="online")
        # 首次/重连上报 M01 + M07（重连时刷新约束与补传水位）
        self._pub_attributes(self.cfg.devinfo(), msg_type="M01")
        self._pub_attributes(self.cfg.constraints(), msg_type="M07")

    def _on_disconnect(self, _c, _u, rc, _p=None):
        was = self.connected
        self.connected = False
        if was:
            self._retry_attempt = 0
            self._next_retry_ts = time.time() + 1.0
            LOGS.add(device=self.cfg.name, direction="evt", result="offline",
                     error=f"rc={rc}")
        # 毒报文防御：服务端异常断链（MQTT_ERR_CONN_LOST）且会话存活极短时，
        # paho 会在每次重连自动重发未确认在途消息；若其中存在服务端判非法的
        # 报文（如坏 JSON），会形成"重发→断链→重连→再重发"死循环，清空在途
        # 待写包与未确认消息队列以打破循环（对端已判定非法，重发无意义）
        now = time.time()
        if now - self._last_online_ts > 30.0:
            self._drop_streak = 0
        elif rc == mqtt.MQTT_ERR_CONN_LOST:
            self._drop_streak += 1
            if self._drop_streak >= 3:
                for attr in ("_out_packet", "_out_messages"):
                    q = getattr(self.cli, attr, None)
                    if q is not None:
                        q.clear()
                self._drop_streak = 0
                LOGS.add(device=self.cfg.name, direction="evt", result="offline",
                         error="断链循环，已清空在途未确认报文（毒报文防御）")

    def _on_message(self, _c, _u, msg):
        try:
            if msg.topic.startswith("v1/devices/me/rpc/request/"):
                self._handle_rpc(msg)
            elif msg.topic == T_ATTRIBUTES:
                self._handle_shared_attributes(msg)
        except Exception as e:  # noqa: BLE001
            log.exception("on_message error")
            LOGS.add(device=self.cfg.name, direction="in", topic=msg.topic,
                     result="error", error=f"{type(e).__name__}: {e}")

    # ---------------- 发布通道（统一异常注入/缓存入口） ----------------
    def _apply_out_faults(self, payload: dict, is_telemetry: bool) -> dict | None:
        """返回实际发送的 payload；None 表示以坏报文替换。"""
        f = self.fault
        if is_telemetry and f["missingRemaining"] > 0:
            f["missingRemaining"] -= 1
            payload = copy.deepcopy(payload)
            payload.get("values", {}).pop(f["missingField"], None)
        if is_telemetry and f["oorRemaining"] > 0:
            f["oorRemaining"] -= 1
            payload = copy.deepcopy(payload)
            payload.get("values", {})[f["oorKey"]] = f["oorValue"]
        return payload

    def _publish(self, topic: str, payload: dict | list, qos: int = 1, *,
                 msg_type: str | None = None, cmd_id: str | None = None,
                 cacheable: tuple[str, dict] | None = None,
                 is_telemetry: bool = False) -> bool:
        """cacheable=(kind, values)：离线时写入缓存而不是丢弃。"""
        f = self.fault
        now = time.time()
        if f["delayUntil"] > now:
            time.sleep(min(f["delayMs"], 2000) / 1000)
        # 数据报文类故障仅作用于遥测报文（M03/M04/M05/M06），不影响心跳与 RPC 应答
        is_data_tele = msg_type in ("M03", "M04", "M05", "M06")
        sent_payload: dict | list | str = self._apply_out_faults(payload, is_data_tele) \
            if isinstance(payload, dict) else payload
        if f["badJsonRemaining"] > 0 and is_data_tele:
            f["badJsonRemaining"] -= 1
            sent_payload = json.dumps(payload)[: max(5, len(json.dumps(payload)) - 3)]  # 截断破坏 JSON
            qos = 0  # 畸形报文不具备 QoS1 确认语义；QoS1 会被 paho 存入在途队列，
            # 服务端断链后每次重连自动重发该毒报文，形成永久断链循环（TB 417 拒收）
        if not self.connected:
            if cacheable:
                ts = payload.get("ts") if isinstance(payload, dict) else int(time.time() * 1000)
                self._cache_append(int(ts or time.time() * 1000), cacheable[0], cacheable[1])
                LOGS.add(device=self.cfg.name, direction="evt", topic=topic,
                         msg_type=msg_type, cmd_id=cmd_id, result="cached")
            return False
        body = sent_payload if isinstance(sent_payload, str) else json.dumps(sent_payload)
        # 入出站队列（socket 写由 worker 线程 _flush_tx 统一执行）
        self._tx_queue.put((topic, body, qos))
        self.sent_count += 1
        LOGS.add(device=self.cfg.name, direction="out", topic=topic, qos=qos,
                 msg_type=msg_type, cmd_id=cmd_id, payload=sent_payload,
                 result="sent")
        if f["dupRemaining"] > 0 and is_data_tele:
            f["dupRemaining"] -= 1
            self._tx_queue.put((topic, body, qos))
            self.sent_count += 1
            LOGS.add(device=self.cfg.name, direction="out", topic=topic, qos=qos,
                     msg_type=msg_type, cmd_id=cmd_id, payload=payload,
                     result="sent-duplicate")
        return True

    def _pub_attributes(self, payload: dict, msg_type: str | None = None):
        self._publish(T_ATTRIBUTES, payload, msg_type=msg_type)

    def _pub_telemetry(self, ts: int, values: dict, *, kind: str,
                       msg_type: str | None = None, cmd_id: str | None = None):
        self._publish(T_TELEMETRY, {"ts": ts, "values": values},
                      msg_type=msg_type or ("M04" if kind == "15m" else "M03"),
                      cacheable=(kind, values), is_telemetry=True)

    def _cache_append(self, ts: int, kind: str, values: dict):
        if len(self.cache) >= CACHE_MAX:
            self.cache_overflow += 1  # 溢出按最旧丢弃（deque 自动）
        self.cache.append({"ts": ts, "kind": kind, "values": values})

    # ---------------- 工作线程：连接管理/周期遥测/心跳/补传 ----------------
    def _worker(self):
        while not self._stop_flag:
            try:
                now_ms = int(time.time() * 1000)
                now = time.time()
                # 网络收发单独隔离：paho 1.6.1 在 _sock.pending()（select 异常
                # 保护之外）可能因内部引用失效持续抛错，若与下方逻辑共用 try
                # 会导致跳过重连/遥测、设备永久静默；此处强制复位传输层后，
                # 下方重连逻辑在同一 tick 内重建连接。
                try:
                    self.cli.loop(0.0)  # 自管网络循环：CONNACK/PUBLISH 下行/PINGRESP
                    self._flush_tx()    # socket 写只在 worker 线程发生（含 publish/断开）
                except Exception as e:  # noqa: BLE001
                    try:
                        self.cli._sock_close()   # 复位传输层（关闭并清空 paho 内部引用）
                    except Exception:  # noqa: BLE001
                        pass
                    if self.connected:
                        self.connected = False
                        self._next_retry_ts = now + 1.0
                        LOGS.add(device=self.cfg.name, direction="evt", result="offline",
                                 error=f"网络循环异常复位: {type(e).__name__}: {e}")
                # 1) 断线注入 / 手动断开（延迟到本线程执行）/ 重连管理
                if self.connected and (self._pending_disconnect
                                       or self.fault["disconnectUntil"] > now):
                    self.cli.disconnect()
                    self._hard_close()
                self._pending_disconnect = False
                if self._want_connect and not self.connected and not self._user_disconnected \
                        and now >= self._next_retry_ts:
                    self._try_connect()
                # 2) 功率推进（每 tick 爬坡）+ 周期遥测（离线时进缓存）
                self._advance_power(now_ms)
                self._tick_periods(now_ms)
                # 3) 业务心跳
                if self.connected and now - self.last_hb_ts >= self.cfg.hbPeriodSec:
                    self._emit_heartbeat(now_ms)
                # 4) 断点补传
                if self.connected:
                    self._tick_replay(now_ms)
                # 5) durationS 到期回退计划功率
                if self.active_target is not None and self._target_until and now >= self._target_until:
                    self.active_target = None
                    self._target_until = None
            except Exception as e:  # noqa: BLE001
                log.exception("worker loop error dev=%s", self.cfg.name)
                if f"{type(e).__name__}:{e}" != self.last_worker_err:
                    self.last_worker_err = f"{type(e).__name__}:{e}"
                    LOGS.add(device=self.cfg.name, direction="evt", result="error",
                             error=f"worker: {self.last_worker_err}")
            time.sleep(0.5)

    def _flush_tx(self):
        """出站队列 flush：paho publish() 直写 socket，仅允许 worker 线程调用。"""
        while True:
            try:
                topic, body, qos = self._tx_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self.cli.publish(topic, body, qos=qos)
            except Exception as e:  # noqa: BLE001
                LOGS.add(device=self.cfg.name, direction="evt", topic=topic, qos=qos,
                         result="error", error=f"flush: {e}")

    def _try_connect(self):
        try:
            self.cli.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
            # inflight 无限：paho 默认 20 条 QoS1 窗口，满时 publish() 会在 worker
            # 线程阻塞等 PUBACK，造成整个 worker 停摆（心跳/遥测全停）。本模拟器
            # 消息量级小，无需窗口流控。
            self.cli.max_inflight_messages_set(float("inf"))
            self.last_error = None
        except Exception as e:  # noqa: BLE001
            self._retry_attempt += 1
            base = min(60.0, 1.0 * (2 ** min(self._retry_attempt, 6)))
            self._next_retry_ts = time.time() + base * random.uniform(0.8, 1.2)
            self.last_error = f"{type(e).__name__}: {e}"

    # ---- 周期边界计算：整分/整刻 +2s 发上周期 ----
    def _tick_periods(self, now_ms: int):
        m_idx = now_ms // 60000
        m_end = m_idx * 60000
        if self.cfg.tele1mEnable and now_ms - m_end >= 2000 and m_end > self.last_1m_end:
            self.last_1m_end = m_end
            vals = self._snapshot(m_end)
            self._pub_telemetry(m_end, vals, kind="1m")
        q_ms = 15 * 60000
        q_idx = now_ms // q_ms
        q_end = q_idx * q_ms
        if self.cfg.tele15mEnable and now_ms - q_end >= 2000 and q_end > self.last_15m_end:
            self.last_15m_end = q_end
            vals = self._snapshot_15m(q_end)
            self._pub_telemetry(q_end, vals, kind="15m")

    def _emit_heartbeat(self, now_ms: int):
        self.last_hb_ts = time.time()
        self.hb_seq = (self.hb_seq + 1) % 2**32
        self._publish(T_TELEMETRY, {
            "ts": now_ms,
            "values": {"msgType": "hb", "hbSeq": self.hb_seq, "runState": self._run_state(),
                       "ctrlMode": self.cfg.ctrlMode,
                       "uptimeSec": (now_ms - self.started_at) // 1000}},
            msg_type="M02", is_telemetry=True)

    # ---- 断点补传：升序、≤100条/包、包间≥200ms、不混包 ----
    def _tick_replay(self, now_ms: int):
        if not self.cache:
            if self._replaying:  # 完成
                self._replaying = False
                self._pub_attributes({"cacheCount": 0}, msg_type="M07-watermark")
                LOGS.add(device=self.cfg.name, direction="evt", msg_type="M06",
                         result="replay-done", error=f"batches={self.replayed_batches}")
            return
        if not self._replaying:
            self._replaying = True
            self.replayed_batches = 0
            self._pub_attributes({"cacheCount": len(self.cache),
                                  "cacheFromTs": self.cache[0]["ts"],
                                  "cacheToTs": self.cache[-1]["ts"]}, msg_type="M07-watermark")
            self._next_batch_ts = now_ms
            return
        if now_ms < self._next_batch_ts:
            return
        batch, kind = [], self.cache[0]["kind"]
        while self.cache and len(batch) < 100 and self.cache[0]["kind"] == kind:
            e = self.cache.popleft()
            batch.append({"ts": e["ts"], "values": e["values"]})
        if batch:
            self._publish(T_TELEMETRY, batch, msg_type="M06", is_telemetry=True)
            self.replayed_batches += 1
        self._next_batch_ts = now_ms + 250  # ≥200ms 包间隔

    # ---------------- 状态/曲线/测点 ----------------
    def _run_state(self) -> int:
        c = self.cfg
        if self.stopped:
            return 5
        if self.executing:
            return 8
        if c.devType == "pv":
            return 2 if self.cur_p > 0.5 else 1
        if c.devType == "pcs":
            return 4 if self.cur_p > 0.5 else (3 if self.cur_p < -0.5 else 1)
        return 2 if abs(self.cur_p) > 0.5 else 1

    def _advance_power(self, ts_ms: int):
        """推进当前功率：计划曲线 / 控制目标（爬坡）/ 限值 / SOC。"""
        c = self.cfg
        if c.devType == "pv":
            plan = curves.pv_power(ts_ms, c.capKw, c.name)
        elif c.devType == "pcs":
            plan = curves.pcs_schedule_target(ts_ms, c.chgPMax, c.disPMax)
            # SOC 边界保护
            if plan > 0 and self.soc <= c.socMin:
                plan = 0.0
            if plan < 0 and self.soc >= c.socMax:
                plan = 0.0
        else:
            plan = curves.flex_power(ts_ms, c.capKw, c.name)
        if self.stopped:
            self.cur_p = 0.0
            return
        target = self.active_target if self.active_target is not None else plan
        if target is None:
            self.cur_p = plan
        else:
            step = c.rampRate * 0.5 / 60.0  # 每 0.5s tick
            diff = target - self.cur_p
            self.cur_p = round(target if abs(diff) <= step else self.cur_p + (step if diff > 0 else -step), 2)
        if c.devType == "pcs" and self.soc is not None:
            cap_e = max(c.capKw * 2.0, 1.0)
            self.soc = round(min(100.0, max(0.0, self.soc - self.cur_p * 0.5 / 3600.0 / cap_e * 100)), 2)

    def _snapshot(self, ts_ms: int) -> dict:
        """M03/M05 实时值（按 devType 键集，additionalProperties=false 严格对齐）。"""
        c = self.cfg
        p = round(min(max(self.cur_p, c.pMin), c.pMax), 2)
        u = round(380.0 + curves._noise(c.name, ts_ms // 60000, 1.0), 1)
        i = round(abs(p) * 1000 / (u * 1.732 * 0.998), 2)
        f = round(50.0 + curves._noise(c.name + "f", ts_ms // 60000, 0.02), 3)
        pf = round(0.99 + abs(curves._noise(c.name + "pf", ts_ms // 60000, 0.005)), 3)
        s = round(abs(p) / pf if pf else 0.0, 2)
        q = round(self.q + curves._noise(c.name + "q", ts_ms // 60000, 2.0), 2)
        vals: dict = {"p": p, "q": q, "u": u, "i": i, "s": s, "pf": pf, "f": f,
                      "t_dev": round(38 + curves._noise(c.name + "t", ts_ms // 60000, 2), 1),
                      "t_amb": round(25 + curves._noise(c.name + "ta", ts_ms // 60000, 3), 1),
                      "runState": self._run_state(), "dqf": 0}
        if c.devType == "pv":
            hour = curves.local_hour_of(ts_ms)
            irradi = round(max(0.0, 1000 * math_sin_pi(hour)) if 6 <= hour < 18 else 0.0, 1)
            dc_u = round(650 + curves._noise(c.name + "du", ts_ms // 60000, 20), 1)
            vals.update({"dc_u": dc_u,
                         "dc_i": round(p * 1000 / max(dc_u, 1), 2),
                         "dc_p": p, "irradi": irradi,
                         "t_mod": round(40 + curves._noise(c.name + "tm", ts_ms // 60000, 4), 1)})
        elif c.devType == "pcs":
            vals.update({"soc": self.soc, "soh": 99.2,
                         "cellVMax": 3.351, "cellVMin": 3.328,
                         "cellTMax": 28.5, "cellTMin": 26.1, "cycCnt": self.cyc_cnt})
        else:
            vals.update({"loadType": 1, "shedState": self.shed_state,
                         "adjUpCap": round(abs(c.pMin) - abs(p), 2),
                         "adjDownCap": round(abs(p), 2)})
        return vals

    def _snapshot_15m(self, ts_ms: int) -> dict:
        """M04 结算口径：功率 + 表码 + 区间增量 + 当日累计。"""
        vals = self._snapshot(ts_ms)
        c = self.cfg
        keep = {k: vals[k] for k in ("u", "i", "p", "pf", "f", "runState") if k in vals}
        keep["dqf"] = 0
        d_imp, d_exp = curves.energy_delta(vals["p"], 0.25)
        self._e_imp = curves.wrap_meter(self._e_imp + d_imp)
        self._e_exp = curves.wrap_meter(self._e_exp + d_exp)
        day_idx = curves.day_index_utc8(ts_ms)
        if day_idx != self._e_day_idx:
            self._e_day_idx, self._e_day = day_idx, 0.0
        self._e_day = round(self._e_day + d_imp + d_exp, 3)
        self.cyc_cnt = 100 + int(self._e_day / max(c.capKw * 2.0, 1.0) * 2)  # 粗略循环次数
        keep["e_imp"], keep["e_exp"] = self._e_imp, self._e_exp
        if self._base_imp is not None:
            keep["e_imp_15"] = round(max(0.0, self._e_imp - self._base_imp), 3)
            keep["e_exp_15"] = round(max(0.0, self._e_exp - self._base_exp), 3)
        if c.devType in ("pv", "pcs"):
            keep["e_day"] = self._e_day
        self._base_imp, self._base_exp = self._e_imp, self._e_exp
        return keep

    # ---------------- RPC 指令闭环（M12→M13→M14） ----------------
    def _respond_rpc(self, req_id: str, resp: dict):
        self._publish(f"v1/devices/me/rpc/response/{req_id}", resp, msg_type="M13",
                      cmd_id=resp.get("cmdId"))

    def _handle_rpc(self, msg):
        req_id = msg.topic.rsplit("/", 1)[-1]
        try:
            frame = json.loads(msg.payload.decode())
            env = frame.get("params", {})
            cmd_type, cmd_id = frame.get("method"), env.get("cmdId")
            params = env.get("params", {})
            now_ms = int(time.time() * 1000)
        except Exception:  # noqa: BLE001
            LOGS.add(device=self.cfg.name, direction="in", topic=msg.topic,
                     msg_type="M12", result="error", error="JSON 解析失败")
            self._respond_rpc(req_id, {"cmdId": "unknown", "state": "REJECTED",
                                       "errCode": 1005, "errMsg": "bad json", "ts": int(time.time() * 1000)})
            return
        LOGS.add(device=self.cfg.name, direction="in", topic=msg.topic, qos=1,
                 msg_type="M12", cmd_id=cmd_id, payload={"method": cmd_type, "envelope": env},
                 result="received")

        def rej(code: int, emsg: str):
            r = {"cmdId": cmd_id, "state": "REJECTED", "errCode": code, "errMsg": emsg[:128],
                 "ts": int(time.time() * 1000)}
            self.ledger.setdefault(cmd_id, {**r, "cmdType": cmd_type})
            self._respond_rpc(req_id, r)

        # ① 过期检查
        if env.get("expireAt") and now_ms > env["expireAt"]:
            return rej(2004, "command expired")
        # ② 幂等：已有终态 → 原终态 + dup；执行中 → ACK
        prev = self.ledger.get(cmd_id)
        if prev:
            if prev["state"] in ("SUCCESS", "FAILED"):
                r = {"cmdId": cmd_id, "state": prev["state"], "errCode": prev["errCode"],
                     "errMsg": prev.get("errMsg", ""), "dup": True,
                     **({"data": prev["data"]} if prev.get("data") else {}),
                     "ts": int(time.time() * 1000)}
                self._respond_rpc(req_id, r)
                self._pub_cmd_status(cmd_id, cmd_type, prev["state"], prev["errCode"],
                                     prev.get("errMsg", ""), dup=True,
                                     extra=prev.get("actualKeys"))
                return
            return self._respond_rpc(req_id, {"cmdId": cmd_id, "state": "ACK", "errCode": 0,
                                              "dup": True, "ts": int(time.time() * 1000)})
        if cmd_type not in QUERY_CMDS and cmd_type not in CTRL_CMDS:
            return rej(1004, f"unsupported cmdType {cmd_type}")
        # ③ 拒绝注入
        if self.fault["rejectRemaining"] > 0:
            self.fault["rejectRemaining"] -= 1
            return rej(2003, "injected reject")
        # ④ 控制类互斥（查询类不受限）
        if cmd_type in CTRL_CMDS and self.executing:
            return rej(2002, f"busy: {self.executing['cmdId']} executing")

        if cmd_type == "readRealtimeData":
            snap = self._snapshot(now_ms)
            keys = params.get("keys") if isinstance(params.get("keys"), list) else None
            data = {k: v for k, v in snap.items() if not keys or k in keys}
            r = {"cmdId": cmd_id, "state": "SUCCESS", "errCode": 0, "data": data,
                 "ts": int(time.time() * 1000)}
            self.ledger[cmd_id] = {"state": "SUCCESS", "errCode": 0, "data": data,
                                   "cmdType": cmd_type, "ts": r["ts"]}
            self._respond_rpc(req_id, r)
            time.sleep(0.3)  # M05 ≤2s 内发出
            self._pub_telemetry(now_ms, data, kind="1m", msg_type="M05")
            return
        if cmd_type == "getDeviceParams":
            allp = {"rampRate": self.cfg.rampRate, "qMin": self.cfg.qMin, "qMax": self.cfg.qMax,
                    "respTimeS": self.cfg.respTimeS, "regMode": self.cfg.regMode,
                    "upTimeS": self.cfg.upTimeS, "hbPeriodSec": self.cfg.hbPeriodSec,
                    "protocolVer": "vpp-1.0", "capKw": self.cfg.capKw}
            keys = params.get("keys") if isinstance(params.get("keys"), list) else None
            data = {k: v for k, v in allp.items() if not keys or k in keys}
            r = {"cmdId": cmd_id, "state": "SUCCESS", "errCode": 0, "data": data,
                 "ts": int(time.time() * 1000)}
            self.ledger[cmd_id] = {"state": "SUCCESS", "errCode": 0, "data": data,
                                   "cmdType": cmd_type, "ts": r["ts"]}
            return self._respond_rpc(req_id, r)
        if cmd_type == "readHistoryData":
            err = self._check_read_history(params)
            if err:
                return rej(*err)
            start_ts, end_ts = params["startTs"], params["endTs"]
            interval = int(params.get("interval", 1))
            step_ms = interval * 60000
            records = []
            t = (start_ts // step_ms) * step_ms
            while t <= end_ts:
                records.append({"ts": t, "values": (self._snapshot_15m(t) if interval == 15
                                                    else self._snapshot(t))})
                t += step_ms
            records = records[-3000:]  # 上限保护
            batches = max(1, -(-len(records) // 100))
            r = {"cmdId": cmd_id, "state": "SUCCESS", "errCode": 0,
                 "data": {"callId": params.get("callId"), "plannedBatches": batches,
                          "records": len(records)},
                 "ts": int(time.time() * 1000)}
            self.ledger[cmd_id] = {"state": "SUCCESS", "errCode": 0, "data": r["data"],
                                   "cmdType": cmd_type, "ts": r["ts"]}
            self._respond_rpc(req_id, r)
            threading.Thread(target=self._replay_history, args=(records,), daemon=True).start()
            return
        # ---- 控制类：参数校验后 ACK + 异步执行 ----
        err = self._validate_control(cmd_type, params)
        if err:
            return rej(*err)
        r = {"cmdId": cmd_id, "state": "ACK", "errCode": 0, "ts": int(time.time() * 1000)}
        self.ledger[cmd_id] = {"state": "ACK", "errCode": 0, "cmdType": cmd_type, "ts": r["ts"]}
        self._respond_rpc(req_id, r)
        self.executing = {"cmdId": cmd_id, "cmdType": cmd_type, "startedMs": r["ts"]}
        self._exec_thread = threading.Thread(target=self._execute_control,
                                             args=(dict(self.executing), params),
                                             name=f"exec-{cmd_id}", daemon=True)
        self._exec_thread.start()

    def _check_read_history(self, params: dict) -> tuple[int, str] | None:
        if not params.get("startTs") or not params.get("endTs"):
            return 1001, "missing startTs/endTs"
        if params["endTs"] <= params["startTs"]:
            return 1002, "endTs must be > startTs"
        if not params.get("callId"):
            return 1001, "missing callId"
        return None

    def _replay_history(self, records: list[dict]):
        """M06 批量回放：升序、≤100 条/包、间隔 ≥200ms。"""
        for i in range(0, len(records), 100):
            if self._stop_flag:
                return
            batch = records[i:i + 100]
            self._publish(T_TELEMETRY, batch, msg_type="M06", is_telemetry=True)
            self.replayed_batches += 1
            time.sleep(0.22)
        LOGS.add(device=self.cfg.name, direction="evt", msg_type="M06",
                 result="history-replay-done", error=f"records={len(records)}")

    def _validate_control(self, cmd_type: str, p: dict) -> tuple[int, str] | None:
        c = self.cfg
        if cmd_type == "setActivePowerTarget":
            tp = p.get("targetP")
            if tp is None:
                return 1001, "missing targetP"
            if not (c.pMin <= float(tp) <= c.pMax):
                return 1002, f"targetP {tp} out of [{c.pMin},{c.pMax}]"
        elif cmd_type == "setMaxPowerLimit":
            mp, scope = p.get("maxP"), p.get("scope")
            if mp is None:
                return 1001, "missing maxP"
            if scope not in ("gen", "chg", "dis"):
                return 1001, "missing/invalid scope"
            if not (0 <= float(mp) <= c.capKw):
                return 1002, f"maxP {mp} out of [0,{c.capKw}]"
            if c.devType == "pv" and scope != "gen":
                return 1002, f"scope {scope} not applicable to pv"
            if c.devType == "flex":
                return 1002, "setMaxPowerLimit not applicable to flex"
        elif cmd_type == "setRunMode":
            if not p.get("action") and not p.get("mode"):
                return 1001, "need action or mode"
            if p.get("action") not in (None, "start", "stop"):
                return 1002, "invalid action"
            if p.get("mode") not in (None, "auto", "manual"):
                return 1002, "invalid mode"
        elif cmd_type == "setChargeDischargePower":
            if c.devType != "pcs":
                return 1002, "only applicable to pcs"
            pw = p.get("p")
            if pw is None:
                return 1001, "missing p"
            pw = float(pw)
            if pw > 0 and (pw > c.disPMax or (self.soc or 0) <= c.socMin):
                return 2003, f"discharge p={pw} over disPMax/SOC floor"
            if pw < 0 and (abs(pw) > c.chgPMax or (self.soc or 100) >= c.socMax):
                return 2003, f"charge p={pw} over chgPMax/SOC ceiling"
        elif cmd_type == "setDeviceParams":
            if not isinstance(p, dict) or not p:
                return 1001, "empty params"
            if len(p) > 20:
                return 1002, "too many keys (>20)"
            bad = [k for k in p if k not in SETTABLE_PARAMS]
            if bad:
                return 1002, f"unsettable keys: {','.join(bad[:5])}"
        return None

    def _execute_control(self, job: dict, params: dict):
        """异步执行：EXECUTING →（爬坡/应用）→ 终态 M14。failCmd 注入 → FAILED。"""
        c, cmd_id, cmd_type = self.cfg, job["cmdId"], job["cmdType"]
        start = time.time()

        def finish(state: str, err_code: int, emsg: str, extra: dict | None = None):
            cost = int((time.time() - start) * 1000)
            self.executing = None
            self.ledger[cmd_id] = {"state": state, "errCode": err_code, "errMsg": emsg,
                                   "cmdType": cmd_type, "ts": int(time.time() * 1000),
                                   "data": None, "actualKeys": extra}
            self._pub_cmd_status(cmd_id, cmd_type, state, err_code, emsg,
                                 extra={"execTs": job["startedMs"], "costMs": cost, **(extra or {})})

        self._pub_cmd_status(cmd_id, cmd_type, "EXECUTING", 0, "",
                             extra={"execTs": job["startedMs"]})
        try:
            if cmd_type == "setActivePowerTarget":
                self._clear_target_timer()
                self.active_target = float(params["targetP"])
                if params.get("durationS"):
                    self._target_until = time.time() + float(params["durationS"])
                self._wait_reached(self.active_target, None)
            elif cmd_type == "setMaxPowerLimit":
                time.sleep(1.0)  # 模拟下发执行
                self.p_limit[params["scope"]] = float(params["maxP"])
            elif cmd_type == "setRunMode":
                time.sleep(1.0)
                if params.get("action") == "stop":
                    self.stopped, self.active_target = True, None
                elif params.get("action") == "start":
                    self.stopped = False
                if params.get("mode"):
                    self.cfg.regMode = params["mode"]
                    self.cfg.ctrlMode = 1 if params["mode"] == "auto" else 0
            elif cmd_type == "setChargeDischargePower":
                self._clear_target_timer()
                self.active_target = float(params["p"])
                if params.get("durationS"):
                    self._target_until = time.time() + float(params["durationS"])
                self._wait_reached(self.active_target, None)
            elif cmd_type == "setDeviceParams":
                time.sleep(0.8)
                for k, v in params.items():
                    setattr(self.cfg, k, type(getattr(self.cfg, k))(v))
                self._pub_attributes(self.cfg.constraints(), msg_type="M07")  # 约束变化即时刷新
                finish("SUCCESS", 0, "", {"applied": params})
                return
            # 执行中失败注入
            if self.fault["failRemaining"] > 0:
                self.fault["failRemaining"] -= 1
                finish("FAILED", 2003, "injected exec failure")
                return
            extra = self._actual_keys(cmd_type)
            finish("SUCCESS", 0, "", extra)
        except Exception as e:  # noqa: BLE001
            finish("FAILED", 1003, f"{type(e).__name__}: {e}")

    def _clear_target_timer(self):
        if self._target_timer:
            self._target_timer.cancel()
            self._target_timer = None

    def _wait_reached(self, target: float, _expire_at):
        """等待功率爬坡到位（rampRate 估算 + 轮询）。"""
        need = abs(target - self.cur_p) / max(self.cfg.rampRate, 0.01) * 60.0
        deadline = time.time() + max(need * 1.25, 1.0)
        while time.time() < deadline:
            if abs(self.cur_p - target) <= max(0.5, abs(target) * 0.01):
                break
            time.sleep(0.4)

    def _actual_keys(self, cmd_type: str) -> dict:
        c = self.cfg
        if cmd_type == "setActivePowerTarget":
            return {"actualP": self.cur_p}
        if cmd_type == "setMaxPowerLimit":
            lim = [v for v in self.p_limit.values() if v is not None]
            return {"actualPLimit": min(lim) if lim else None, "actualP": self.cur_p}
        if cmd_type == "setRunMode":
            return {"runState": self._run_state()}
        if cmd_type == "setChargeDischargePower":
            return {"actualP": self.cur_p, "soc": self.soc}
        return {}

    def _pub_cmd_status(self, cmd_id: str, cmd_type: str, state: str, err_code: int,
                        emsg: str, *, dup: bool = False, extra: dict | None = None):
        vals = {"msgType": "cmdStatus", "cmdId": cmd_id, "cmdType": cmd_type,
                "cmdState": state, "errCode": err_code, "errMsg": emsg[:128]}
        if dup:
            vals["dup"] = True
        for k, v in (extra or {}).items():
            if v is not None:
                vals[k] = v
        self._publish(T_TELEMETRY, {"ts": int(time.time() * 1000), "values": vals},
                      msg_type="M14", cmd_id=cmd_id, is_telemetry=True)

    # ---------------- 共享属性（M08 → 回执） ----------------
    def _handle_shared_attributes(self, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:  # noqa: BLE001
            LOGS.add(device=self.cfg.name, direction="in", topic=msg.topic,
                     msg_type="M08", result="error", error="JSON 解析失败")
            return
        LOGS.add(device=self.cfg.name, direction="in", topic=msg.topic, qos=1,
                 msg_type="M08", payload=payload, result="received")
        result, err = self._apply_shared(payload)
        report = {"cfgVerApplied": self.cfg.cfgVer, "cfgApplyTs": int(time.time() * 1000),
                  "cfgResult": result}
        if err:
            report["cfgApplyErr"] = err[:64]
        LOGS.add(device=self.cfg.name, direction="out", topic=T_ATTRIBUTES,
                 msg_type="M08-ack", payload=report, result=("ok" if result == 0 else "rejected"))
        self._pub_attributes(report, msg_type="M08-ack")

    def _apply_shared(self, payload: dict) -> tuple[int, str]:
        """整体原子校验：全部合法才应用（含 cfgVer 回退/重复拒绝）。"""
        c = self.cfg
        cfg_ver = payload.get("cfgVer")
        if not isinstance(cfg_ver, int) or cfg_ver < 1:
            return 1002, "cfgVer invalid"
        if cfg_ver <= c.cfgVer:
            return 1002, f"cfgVer {cfg_ver} not newer than applied {c.cfgVer}"
        hb = payload.get("hbPeriodSec", c.hbPeriodSec)
        if not isinstance(hb, int) or not (10 <= hb <= 300):
            return 1002, "hbPeriodSec must be 10..300"
        for k in ("tele1mEnable", "tele15mEnable"):
            if k in payload and not isinstance(payload[k], bool):
                return 1002, f"{k} must be bool"
        if "logLevel" in payload and payload["logLevel"] not in ("debug", "info", "warn", "error"):
            return 1002, "logLevel invalid"
        # 全部合法 → 应用
        c.cfgVer = cfg_ver
        c.hbPeriodSec = hb
        for k in ("tele1mEnable", "tele15mEnable"):
            if k in payload:
                setattr(c, k, payload[k])
        return 0, ""


def math_sin_pi(hour: float) -> float:
    import math
    return math.sin(math.pi * (hour - 6.0) / 12.0)


class DeviceManager:
    def __init__(self, *, mqtt_host: str, mqtt_port: int, ca_file: str,
                 cert_dir: str, workspace_root: str):
        self.mqtt_host, self.mqtt_port = mqtt_host, mqtt_port
        self.ca_file = ca_file
        self.cert_dir = cert_dir
        self.workspace_root = workspace_root
        self.devices: dict[str, DeviceSim] = {}

    def cert_paths(self, name: str) -> tuple[str, str, str]:
        d = f"{self.cert_dir}/{name}"
        return self.ca_file, f"{d}/cert.pem", f"{d}/key.pem"

    def create(self, cfg: DeviceConfig, *, auto_start: bool = True) -> DeviceSim:
        if cfg.name in self.devices:
            raise ValueError(f"设备已存在: {cfg.name}")
        ca, cert, key = self.cert_paths(cfg.name)
        dev = DeviceSim(cfg, mqtt_host=self.mqtt_host, mqtt_port=self.mqtt_port,
                        ca_file=ca, cert_file=cert, key_file=key)
        self.devices[cfg.name] = dev
        LOGS.add(device=cfg.name, direction="evt", result="created")
        if auto_start:
            dev.start_connect()
        return dev

    def delete(self, name: str) -> bool:
        dev = self.devices.pop(name, None)
        if dev:
            dev.shutdown()
            LOGS.add(device=name, direction="evt", result="deleted")
        return dev is not None

    def get(self, name: str) -> DeviceSim | None:
        return self.devices.get(name)

    def shutdown_all(self):
        for d in self.devices.values():
            d.shutdown()
        self.devices.clear()

    def summary(self) -> list[dict]:
        return [d.status() for d in self.devices.values()]
