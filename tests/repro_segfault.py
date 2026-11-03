"""最小复现：自管 paho loop（loop(0) 由调用线程驱动）反复连接/断开 18885。
若段错误复现，则在崩溃前打印各阶段标记以定位。

用法：python tests/repro_segfault.py [轮数]
"""
import json
import ssl
import sys
import time
import paho.mqtt.client as mqtt

HOST, PORT = "localhost", 18885
CA = "/Users/wuchao/本地工作/thingsboard-dev/certmgr/secrets/root/ca.pem"
CHAIN = "/Users/wuchao/本地工作/thingsboard-dev/certmgr/secrets/intermediate/ca-chain.pem"
CERT = "/Users/wuchao/本地工作/thingsboard-dev/certmgr/secrets/devices/pv-1001/cert.pem"
KEY = "/Users/wuchao/本地工作/thingsboard-dev/certmgr/secrets/devices/pv-1001/key.pem"

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
connected = False


def on_connect(c, u, f, rc, props=None):
    global connected
    connected = True
    print(f"    on_connect rc={rc}", flush=True)


def on_disconnect(c, u, rc, props=None):
    global connected
    connected = False
    print(f"    on_disconnect rc={rc}", flush=True)


def main():
    for i in range(rounds):
        print(f"[round {i}] 创建 client", flush=True)
        cli = mqtt.Client(client_id=f"repro-{i}", protocol=mqtt.MQTTv311)
        cli.on_connect, cli.on_disconnect = on_connect, on_disconnect
        cli.tls_set(ca_certs=CA, certfile=CERT, keyfile=KEY,
                    cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
        print("  connect()", flush=True)
        cli.connect(HOST, PORT, keepalive=60)
        t0 = time.time()
        while not connected and time.time() - t0 < 5:
            cli.loop(0.05)  # 等 CONNACK
            time.sleep(0.05)
        assert connected, "未上线"
        print("  publish", flush=True)
        cli.publish("v1/devices/me/telemetry",
                    json.dumps({"ts": int(time.time() * 1000), "values": {"p": 1}}), qos=1)
        cli.loop(0.1)
        print("  disconnect()", flush=True)
        cli.disconnect()
        t0 = time.time()
        while connected and time.time() - t0 < 3:
            cli.loop(0.05)
            time.sleep(0.05)
        print(f"  round {i} 完成", flush=True)
    print("全部轮次完成，无崩溃", flush=True)


if __name__ == "__main__":
    main()
