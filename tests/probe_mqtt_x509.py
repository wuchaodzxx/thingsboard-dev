"""MQTT X.509 接入探针：用指定证书连接 TB MQTTS 并发布一条遥测。

用法：
  python probe_mqtt_x509.py --ca ROOT.pem --cert cert.pem --key key.pem \
      [--host localhost] [--port 18885] [--device-name pv-0001] [--timeout 15]
输出 JSON：{connected, published, error}；退出码 0=接入成功，1=失败。
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time

import paho.mqtt.client as mqtt

RC_DESC = {
    0: "连接成功", 1: "协议版本错误", 2: "clientId 非法", 3: "服务不可用",
    4: "用户名/密码错误（TB: 认证失败）", 5: "未授权",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=18885)
    ap.add_argument("--ca", required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--device-name", default="probe")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--keepalive", type=int, default=60)
    args = ap.parse_args()

    result = {"connected": False, "published": False, "error": None}
    connected_evt = {"ok": False}
    pub_evt = {"ok": False}

    c = mqtt.Client(client_id=f"probe-{args.device_name}-{int(time.time())}",
                    protocol=mqtt.MQTTv311)

    def on_connect(cl, u, f, rc, props=None):
        connected_evt["ok"] = (rc == 0)
        result["error"] = None if rc == 0 else f"CONNACK rc={rc} {RC_DESC.get(rc, '')}"

    def on_publish(cl, u, mid, rc=None, props=None):
        pub_evt["ok"] = True

    c.on_connect = on_connect
    c.on_publish = on_publish
    try:
        c.tls_set(ca_certs=args.ca, certfile=args.cert, keyfile=args.key,
                  cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        c.connect(args.host, args.port, keepalive=args.keepalive)
        c.loop_start()
        t0 = time.time()
        while time.time() - t0 < args.timeout and not connected_evt["ok"]:
            time.sleep(0.2)
        result["connected"] = connected_evt["ok"]
        if not connected_evt["ok"]:
            if result["error"] is None:
                result["error"] = "TLS 握手/连接超时（无 CONNACK）"
        else:
            payload = {"ts": int(time.time() * 1000),
                       "values": {"msgType": "probe", "probeTs": int(time.time() * 1000)}}
            info = c.publish("v1/devices/me/telemetry", json.dumps(payload), qos=1)
            t1 = time.time()
            while time.time() - t1 < 10 and not pub_evt["ok"]:
                time.sleep(0.1)
            result["published"] = pub_evt["ok"] and info.rc == mqtt.MQTT_ERR_SUCCESS
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            c.disconnect()
        except Exception:  # noqa: BLE001
            pass
        c.loop_stop()

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["connected"] and result["published"] else 1


if __name__ == "__main__":
    sys.exit(main())
