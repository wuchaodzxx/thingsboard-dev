"""ThingsBoard CE 3.5.1 REST 客户端：设备/配置档案/X.509 凭据/遥测查询。"""
from __future__ import annotations

import json
from typing import Any

import requests


class TBClient:
    def __init__(self, base_url: str = "http://localhost:18080",
                 username: str = "tenant@thingsboard.org",
                 password: str = "tenant", timeout: int = 20):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.token: str | None = None

    # ---------- auth ----------
    def login(self) -> str:
        r = requests.post(f"{self.base}/api/auth/login",
                          json={"username": self.username, "password": self.password},
                          timeout=self.timeout)
        r.raise_for_status()
        self.token = r.json()["token"]
        return self.token

    def _h(self) -> dict:
        if not self.token:
            self.login()
        return {"X-Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"}

    def _req(self, method: str, path: str, **kw) -> Any:
        r = requests.request(method, f"{self.base}{path}", headers=self._h(),
                             timeout=self.timeout, **kw)
        if r.status_code == 401:  # token 过期重登一次
            self.token = None
            r = requests.request(method, f"{self.base}{path}", headers=self._h(),
                                 timeout=self.timeout, **kw)
        if not r.ok:
            raise RuntimeError(f"TB {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # ---------- device profile / device ----------
    def get_device_profile(self, name: str) -> dict | None:
        try:
            return self._req("GET", f"/api/deviceProfile/info/{name}")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def create_device_profile(self, name: str, description: str = "") -> dict:
        body = {"name": name, "type": "DEFAULT", "transportType": "DEFAULT",
                "provisionType": "DISABLED", "description": description,
                "isDefault": False}
        return self._req("POST", "/api/deviceProfile", json=body)

    def ensure_device_profile(self, name: str, description: str = "") -> dict:
        p = self.get_device_profile(name)
        return p if p else self.create_device_profile(name, description)

    def get_device(self, name: str) -> dict | None:
        try:
            return self._req("GET", f"/api/tenant/devices?deviceName={name}")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def get_device_info(self, device_id: str) -> dict:
        """设备详情（含 active 在线标志；本 TB 版本 /api/device/{id} 与列表接口均不含）。"""
        return self._req("GET", f"/api/device/info/{device_id}")

    def create_device(self, name: str, dev_type: str = "default") -> dict:
        # 3.5.1：按 type 创建设备，平台自动 findOrCreate 同名设备配置档案（实测验证）
        body: dict = {"name": name, "type": dev_type}
        return self._req("POST", "/api/device", json=body)

    def delete_device(self, device_id: str) -> None:
        self._req("DELETE", f"/api/device/{device_id}")

    # ---------- credentials ----------
    def get_credentials(self, device_id: str) -> dict:
        return self._req("GET", f"/api/device/{device_id}/credentials")

    def set_x509_credentials(self, device_id: str, cert_pem: str) -> dict:
        cred = self.get_credentials(device_id)
        cred["credentialsType"] = "X509_CERTIFICATE"
        cred["credentialsValue"] = cert_pem
        # 3.5.1 实测：更新凭据为 POST（PUT 返回 405）
        return self._req("POST", "/api/device/credentials", json=cred)

    def reset_credentials_to_access_token(self, device_id: str) -> dict:
        """吊销落地（3.5.1 无删除凭据端点，实测 DELETE 404）：
        将凭据整体重置为 ACCESS_TOKEN + 随机值，原 X.509 证书随即无法接入。"""
        import uuid
        cred = self.get_credentials(device_id)
        cred["credentialsType"] = "ACCESS_TOKEN"
        cred["credentialsValue"] = uuid.uuid4().hex
        return self._req("POST", "/api/device/credentials", json=cred)

    # ---------- telemetry / attributes ----------
    def get_telemetry_keys(self, device_id: str) -> list[str]:
        r = self._req("GET",
                      f"/api/plugins/telemetry/DEVICE/{device_id}/keys/timeseries")
        return [k["key"] for k in r]

    def get_latest_timeseries(self, device_id: str, keys: str = "") -> dict:
        path = f"/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries"
        if keys:
            path += f"?keys={keys}"
        return self._req("GET", path)

    def get_history_timeseries(self, device_id: str, keys: str,
                               start_ts: int, end_ts: int, limit: int = 1000) -> dict:
        q = f"?keys={keys}&startTs={start_ts}&endTs={end_ts}&limit={limit}"
        return self._req("GET",
                         f"/api/plugins/telemetry/DEVICE/{device_id}/values/timeseries{q}")

    def get_attributes(self, device_id: str, scope: str = "CLIENT_SCOPE") -> dict:
        return self._req("GET",
                         f"/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/{scope}")

    def save_shared_attributes(self, device_id: str, payload: dict) -> None:
        self._req("POST",
                  f"/api/plugins/telemetry/DEVICE/{device_id}/SHARED_SCOPE",
                  json=payload)

    # ---------- rpc ----------
    def send_two_way_rpc(self, device_id: str, method: str, params: dict,
                         timeout_ms: int = 10000) -> dict:
        body = {"method": method, "params": params, "timeout": timeout_ms}
        return self._req("POST", f"/api/plugins/rpc/twoway/{device_id}", json=body)

    def save_device_attributes_client(self, device_id: str, payload: dict) -> None:
        """调试辅助：平台侧写客户端属性。"""
        self._req("POST",
                  f"/api/plugins/telemetry/DEVICE/{device_id}/CLIENT_SCOPE",
                  json=payload)

    # ---------- 组合操作 ----------
    def register_device_with_cert(self, name: str, cert_pem: str,
                                  dev_type: str = "default") -> dict:
        """创建设备并绑定 X.509 凭据（证书 PEM 作为凭据值）。"""
        dev = self.get_device(name)
        if dev is None:
            dev = self.create_device(name, dev_type)
        self.set_x509_credentials(dev["id"]["id"], cert_pem)
        return dev

    def dump(self, obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)
