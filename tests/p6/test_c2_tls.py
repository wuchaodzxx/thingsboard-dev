"""P6 C2 X.509 双向 TLS 认证成功及失败（3 例）。

前置条件：TB MQTT over TLS :18885 监听中；pv-1001 设备证书/私钥存在于 certmgr/secrets。
用例通过真实 TLS 握手验证（ssl socket 直连 MQTT 端口），失败用例预期握手被拒绝。
"""
from __future__ import annotations

import socket
import ssl
from pathlib import Path

import pytest

from conftest import CA_CHAIN, SECRETS

WS_DIR = Path(__file__).resolve().parents[2]
TMP = WS_DIR / "tests" / "p6" / "_tmp_tls"

GOOD_CERT = SECRETS / "devices" / "pv-1001" / "cert.pem"
GOOD_KEY = SECRETS / "devices" / "pv-1001" / "key.pem"
HOST, PORT = "localhost", 18885


def handshake(cert: Path | None, key: Path | None, cafile: Path = CA_CHAIN,
              timeout: float = 8.0) -> tuple[bool, str]:
    """对 TB MQTT TLS 端口发起真实握手，返回 (是否成功, 版本或错误信息)。"""
    ctx = ssl.create_default_context(cafile=str(cafile))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED            # 校验服务器证书（由我方 CA 签发）
    if cert:
        ctx.load_cert_chain(str(cert), str(key))   # 双向 TLS：提供客户端证书
    with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        try:
            with ctx.wrap_socket(sock, server_hostname=HOST) as ss:
                return True, f"TLS 握手成功 {ss.version()}"
        except ssl.SSLError as e:
            return False, f"SSLError: {e.reason or e}"
        except (ConnectionResetError, OSError) as e:
            return False, f"{type(e).__name__}: {e}"


@pytest.mark.functional
def test_c2_01_valid_cert_accepted(rec):
    """合法证书：链内设备证书 + 私钥 → 握手成功（双向 TLS 通过）。"""
    rec(tcid="TC-C2-01", name="X.509 双向 TLS：合法证书接入成功",
        precondition="TB :18885 监听中；pv-1001 持有链内证书",
        steps=f"ssl 握手：client={GOOD_CERT.name}，CA={CA_CHAIN.name}",
        expected="握手成功（TLSv1.2/1.3）")
    ok, msg = handshake(GOOD_CERT, GOOD_KEY)
    assert ok, msg
    rec(detail=msg)


@pytest.mark.functional
def test_c2_02_rogue_ca_rejected(rec):
    """伪造 CA 证书无法完成接入。

    TB 3.5.1 实际行为（记录实际结果）：MqttSslHandlerProvider 硬编码 WANT 模式，
    TLS 握手层不校验客户端证书链——rogue 证书握手可建立；证书信任在 MQTT
    CONNECT 层按证书指纹检索凭据强制，未注册指纹的 CONNECT 必须被拒绝。
    """
    rec(tcid="TC-C2-02", name="X.509 双向 TLS：非信任 CA 证书无法完成接入",
        precondition="同 TC-C2-01",
        steps="openssl 现场生成 rogue 根 CA 并签发设备证书 → 携该证书握手 → "
              "发送 MQTT CONNECT → 观察平台响应",
        expected="rogue 证书设备无法完成接入：CONNECT 被拒（CONNACK rc≠0）或连接被关闭")
    from certmgr.openssl_tool import (device_cert_ext, gen_key, make_csr,
                                      self_signed_root_ca, sign_csr)
    TMP.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = TMP / "rogue_ca_key.pem", TMP / "rogue_ca.pem"
    k, csr, c = TMP / "rogue_key.pem", TMP / "rogue.csr", TMP / "rogue_cert.pem"
    gen_key(ca_key)
    self_signed_root_ca(ca_key, ca_cert, "/CN=rogue-vpp-test", days=1)
    gen_key(k)
    make_csr(k, csr, "/CN=rogue-device")
    sign_csr(csr, ca_cert, ca_key, c, days=1, ext_text=device_cert_ext("rogue-device"))
    ok, msg = handshake(c, k)
    assert ok, f"rogue 证书握手失败（TB 为 WANT 模式，预期握手可建立）: {msg}"
    detail = _mqtt_connect_reject_probe(client_id=b"p6-rogue", cert=c, key=k)
    rec(detail=f"握手 {msg}（WANT 模式实际行为，TLS 层不校验链）；{detail}")


def _mqtt_connect_reject_probe(client_id: bytes = b"p6-nocert", timeout: float = 8.0,
                               cert: Path | None = None, key: Path | None = None) -> str:
    """TLS 握手后发送 MQTT CONNECT（可选携带证书），返回平台拒绝方式的描述。"""
    ctx = ssl.create_default_context(cafile=str(CA_CHAIN))
    ctx.check_hostname = False
    if cert:
        ctx.load_cert_chain(str(cert), str(key))
    with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        with ctx.wrap_socket(sock, server_hostname=HOST) as ss:
            # MQTTv311 CONNECT：协议名/级别4 + clean session + keepalive60 + clientId
            vh = (b"\x00\x04MQTT\x04\x02\x00\x3c"
                  + len(client_id).to_bytes(2, "big") + client_id)
            ss.sendall(b"\x10" + len(vh).to_bytes(1, "big") + vh)
            resp = b""
            while len(resp) < 2:
                try:
                    chunk = ss.recv(2 - len(resp))
                except (ConnectionResetError, ssl.SSLError, socket.timeout):
                    break
                if not chunk:
                    break
                resp += chunk
    if not resp:
        return "无证书 CONNECT 后连接被服务端关闭（拒绝接入）"
    assert resp[0] >> 4 == 2, f"非 CONNACK 响应: {resp.hex()}"
    rc = resp[1]
    assert rc != 0, "无证书 CONNECT 竟被平台接受（rc=0），认证失守"
    codes = {2: "身份验证失败", 3: "服务器不可用", 4: "用户名/密码错误",
             5: "未授权"}
    return f"CONNACK rc={rc}（{codes.get(rc, '拒绝')}，拒绝接入）"


@pytest.mark.functional
def test_c2_03_no_client_cert_rejected(rec):
    """无客户端证书接入被拒。

    TB 3.5.1 实际行为（记录实际结果）：MqttSslHandlerProvider 硬编码
    setNeedClientAuth(false)+setWantClientAuth(true)——TLS 握手层不强制客户端
    证书（无配置项）；设备认证在 MQTT CONNECT 层强制，无证书凭据的 CONNECT
    必须被平台拒绝。本例以"无证书设备能否完成接入"为判定标准。
    """
    rec(tcid="TC-C2-03", name="X.509 双向 TLS：无客户端证书接入被拒绝",
        precondition="同 TC-C2-01",
        steps="ssl 握手不加载客户端证书（TB WANT 模式，握手可建立）→ "
              "发送无认证凭据的 MQTT CONNECT → 观察平台响应",
        expected="无证书设备无法完成接入：CONNECT 被拒（CONNACK rc≠0）或连接被关闭")
    ok, msg = handshake(None, None)
    assert ok, f"无客户端证书握手失败（TB 为 WANT 模式，预期握手可建立）: {msg}"
    detail = _mqtt_connect_reject_probe()
    rec(detail=f"握手 {msg}（WANT 模式实际行为）；{detail}")
