"""PKI 高层管理：根CA/中间CA/服务器证书/设备证书、登记簿、吊销标记。

目录布局（base_dir 默认 certmgr/secrets，不入代码仓库）：
  base_dir/
  ├── root/          ca.pem ca_key.pem ca.srl
  ├── intermediate/  ca.pem ca_key.pem ca-chain.pem(中间+根)
  ├── registry.json  签发登记簿（名称/序列号/有效期/吊销状态）
  └── devices/<name>/ key.pem csr.pem cert.pem fullchain.pem
服务器证书输出目录由调用方指定（默认 deploy/certs/server）。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import openssl_tool as ossl

DEF_ROOT_CN = "VPP Root CA"
DEF_INT_CN = "VPP Intermediate CA 01"
DEF_ORG = "VPP-Lab"


class PKI:
    def __init__(self, base_dir: Path):
        self.base = Path(base_dir)
        self.root_dir = self.base / "root"
        self.int_dir = self.base / "intermediate"
        self.dev_dir = self.base / "devices"
        self.registry_file = self.base / "registry.json"

    # ---------- registry ----------
    def _registry(self) -> dict:
        if self.registry_file.exists():
            return json.loads(self.registry_file.read_text())
        return {"issued": {}}

    def _save_registry(self, reg: dict) -> None:
        self.registry_file.parent.mkdir(parents=True, exist_ok=True)
        self.registry_file.write_text(json.dumps(reg, ensure_ascii=False, indent=2))

    def _record(self, name: str, kind: str, cert: Path, files: dict, **extra) -> None:
        reg = self._registry()
        info = ossl.info(cert)
        reg["issued"][name] = {
            "kind": kind,
            "cn": info.get("cn", ""),
            "serial": info.get("serial", ""),
            "notBefore": info.get("notBefore", ""),
            "notAfter": info.get("notAfter", ""),
            "files": {k: str(v) for k, v in files.items()},
            "revokedAt": None,
            **extra,
        }
        self._save_registry(reg)

    # ---------- CA ----------
    def init_ca(
        self,
        org: str = DEF_ORG,
        root_cn: str = DEF_ROOT_CN,
        int_cn: str = DEF_INT_CN,
        days_root: int = 7300,
        days_int: int = 7300,
        force: bool = False,
    ) -> dict:
        root_cert = self.root_dir / "ca.pem"
        if root_cert.exists() and not force:
            raise FileExistsError(f"CA 已存在：{root_cert}（--force 覆盖将作废全部已签发证书）")
        for d in (self.root_dir, self.int_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 根 CA
        root_key = self.root_dir / "ca_key.pem"
        ossl.gen_key(root_key, 4096)
        ossl.self_signed_root_ca(root_key, root_cert, f"/O={org}/CN={root_cn}", days_root)
        # 中间 CA
        int_key = self.int_dir / "ca_key.pem"
        int_csr = self.int_dir / "ca.csr"
        int_cert = self.int_dir / "ca.pem"
        ossl.gen_key(int_key, 4096)
        ossl.make_csr(int_key, int_csr, f"/O={org}/CN={int_cn}")
        ossl.sign_csr(int_csr, root_cert, root_key, int_cert, days_int, ossl.intermediate_ca_ext())
        chain = self.int_dir / "ca-chain.pem"
        chain.write_text(int_cert.read_text() + root_cert.read_text())
        self._record(root_cn, "root-ca", root_cert, {"cert": root_cert, "key": root_key}, org=org)
        self._record(int_cn, "intermediate-ca", int_cert,
                     {"cert": int_cert, "key": int_key, "chain": chain}, org=org)
        return {"root": str(root_cert), "intermediate": str(int_cert), "chain": str(chain)}

    def ca_paths(self) -> dict:
        return {
            "root_cert": self.root_dir / "ca.pem",
            "root_key": self.root_dir / "ca_key.pem",
            "int_cert": self.int_dir / "ca.pem",
            "int_key": self.int_dir / "ca_key.pem",
            "chain": self.int_dir / "ca-chain.pem",
        }

    # ---------- 服务器证书 ----------
    def issue_server_cert(
        self, out_dir: Path, name: str = "server", days: int = 7300,
        dns: list[str] | None = None, ips: list[str] | None = None,
        org: str = DEF_ORG,
    ) -> dict:
        ca = self.ca_paths()
        for k, p in ca.items():
            if not Path(p).exists():
                raise FileNotFoundError(f"缺少 CA 文件 {p}，请先 init-ca")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        key, csr = out_dir / f"{name}_key.pem", out_dir / f"{name}.csr"
        cert, fullchain = out_dir / f"{name}.pem", out_dir / f"{name}-fullchain.pem"
        ossl.gen_key(key)
        ossl.make_csr(key, csr, f"/O={org}/CN={name}")
        ossl.sign_csr(csr, ca["int_cert"], ca["int_key"], cert, days,
                      ossl.server_cert_ext(dns or ["localhost"], ips or ["127.0.0.1"]))
        fullchain.write_text(cert.read_text() + (ca["int_cert"]).read_text())
        self._record(name, "server", cert,
                     {"cert": cert, "key": key, "fullchain": fullchain, "csr": csr},
                     dns=dns, ips=ips, outDir=str(out_dir))
        return {"cert": str(cert), "key": str(key), "fullchain": str(fullchain)}

    # ---------- 设备证书 ----------
    def issue_device_cert(self, name: str, days: int = 7300, org: str = DEF_ORG,
                          out_dir: Path | None = None) -> dict:
        ca = self.ca_paths()
        d = Path(out_dir) if out_dir else self.dev_dir / name
        d.mkdir(parents=True, exist_ok=True)
        key, csr = d / "key.pem", d / "csr.pem"
        cert, fullchain = d / "cert.pem", d / "fullchain.pem"
        ossl.gen_key(key)
        ossl.make_csr(key, csr, f"/O={org}/CN={name}")
        ossl.sign_csr(csr, ca["int_cert"], ca["int_key"], cert, days, ossl.device_cert_ext(name))
        fullchain.write_text(cert.read_text() + (ca["int_cert"]).read_text())
        self._record(name, "device", cert,
                     {"cert": cert, "key": key, "fullchain": fullchain, "csr": csr})
        return {"name": name, "cert": str(cert), "key": str(key),
                "fullchain": str(fullchain), "dir": str(d)}

    def sign_external_csr(self, csr: Path, name: str, days: int = 7300,
                          out_dir: Path | None = None) -> dict:
        """厂家自带密钥对与 CSR：只签发证书，私钥不出厂家环境。

        先校验 CSR 可解析（openssl req -noout），无效 CSR 直接拒绝，
        避免先建产物目录再签发失败留下半成品目录。
        """
        r = subprocess.run(["openssl", "req", "-in", str(csr), "-noout"],
                           capture_output=True)
        if r.returncode != 0:
            raise ValueError("CSR 解析失败: "
                             + r.stderr.decode(errors="replace").strip()[:200])
        ca = self.ca_paths()
        d = Path(out_dir) if out_dir else self.dev_dir / name
        d.mkdir(parents=True, exist_ok=True)
        cert, fullchain = d / "cert.pem", d / "fullchain.pem"
        ossl.sign_csr(Path(csr), ca["int_cert"], ca["int_key"], cert, days,
                      ossl.device_cert_ext(name))
        fullchain.write_text(cert.read_text() + (ca["int_cert"]).read_text())
        self._record(name, "device(csr)", cert, {"cert": cert, "fullchain": fullchain},
                     sourceCsr=str(csr))
        return {"name": name, "cert": str(cert), "fullchain": str(fullchain), "dir": str(d)}

    def import_device_cert(self, name: str, cert_pem: str,
                           key_pem: str | None = None) -> dict:
        """手动配置证书：导入外部已有设备证书（PEM）并登记入册，私钥可选一并落盘。

        适用场景：证书由第三方/厂家侧预先持有，平台侧仅登记证书用于信任校验；
        模拟器本机连接需私钥（key.pem），厂家侧持有私钥时不导入。
        """
        cert_pem = (cert_pem or "").strip()
        if "-----BEGIN CERTIFICATE-----" not in cert_pem:
            raise ValueError("certPem 不是 PEM 证书（缺少 BEGIN CERTIFICATE）")
        d = self.dev_dir / name
        d.mkdir(parents=True, exist_ok=True)
        cert, fullchain = d / "cert.pem", d / "fullchain.pem"
        cert.write_text(cert_pem + "\n")
        info = ossl.info(cert)
        if not info.get("cn"):
            raise ValueError("证书解析失败或缺少 CN")
        chain = self.int_dir / "ca-chain.pem"
        fullchain.write_text(cert_pem + "\n"
                             + (chain.read_text() if chain.exists() else ""))
        files = {"cert": cert, "fullchain": fullchain}
        key_imported = False
        if key_pem and "PRIVATE KEY" in key_pem:
            key = d / "key.pem"
            key.write_text(key_pem.strip() + "\n")
            files["key"] = key
            key_imported = True
        self._record(name, "device(import)", cert, files, source="manual-import")
        return {"name": name, "cn": info.get("cn", ""), "serial": info.get("serial", ""),
                "notAfter": info.get("notAfter", ""), "dir": str(d),
                "keyImported": key_imported}

    # ---------- 线下交付 ----------
    def mark_delivered(self, name: str, destroy_key: bool = False) -> dict:
        """线下交付台账：记录交付时间；可选销毁平台侧私钥（交付完成后推荐）。

        TB X.509 凭据注册与设备 TLS 握手只需证书，私钥交付后平台侧无需继续持有。
        CSR 签发（sourceCsr）路径本无私钥，destroy_key 不生效，仅记录交付时间。
        """
        reg = self._registry()
        if name not in reg["issued"]:
            raise KeyError(f"登记簿中不存在：{name}")
        rec = reg["issued"][name]
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        rec["deliveredAt"] = now
        key_removed = False
        key_file = (rec.get("files") or {}).get("key")
        if destroy_key and key_file and Path(key_file).exists():
            Path(key_file).unlink()
            rec["files"].pop("key", None)
            rec["keyDestroyedAt"] = now
            key_removed = True
        self._save_registry(reg)
        return {"name": name, "deliveredAt": now, "keyDestroyed": key_removed}

    # ---------- 校验/查询/吊销 ----------
    def verify_device(self, name: str) -> str:
        cert = self.dev_dir / name / "cert.pem"
        chain = self.int_dir / "ca-chain.pem"
        return ossl.verify(cert, self.root_dir / "ca.pem", chain)

    def verify_cert_files(self, cert: Path, chain: Path | None = None) -> str:
        return ossl.verify(Path(cert), self.root_dir / "ca.pem",
                           *( [Path(chain)] if chain else [self.int_dir / "ca-chain.pem"] ))

    def list_certs(self) -> list[dict]:
        reg = self._registry()
        return [
            {"name": k, **v}
            for k, v in sorted(reg["issued"].items())
        ]

    def revoke(self, name: str, reason: str = "unspecified") -> dict:
        reg = self._registry()
        if name not in reg["issued"]:
            raise KeyError(f"登记簿中不存在：{name}")
        reg["issued"][name]["revokedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        reg["issued"][name]["revokeReason"] = reason
        self._save_registry(reg)
        return {"name": name, "revoked": True, "reason": reason,
                "note": "TB CE 3.5.1 不消费 CRL：平台侧生效须同步删除 TB 设备 X.509 凭据（tb-revoke-device）"}

    def check_expiry(self, within_days: int = 30) -> list[dict]:
        """解析 notAfter 并比较（OpenSSL 格式如 'notAfter=Jun  1 00:00:00 2027 GMT'）。"""
        import datetime

        soon = []
        for c in self.list_certs():
            na = c.get("notAfter", "")
            try:
                dt = datetime.datetime.strptime(na, "%b %d %H:%M:%S %Y %Z")
            except ValueError:
                continue
            left = (dt - datetime.datetime.utcnow()).days
            if left <= within_days:
                soon.append({"name": c["name"], "notAfter": na, "daysLeft": left,
                             "revokedAt": c.get("revokedAt")})
        return soon

    def cert_pem(self, name: str) -> str:
        files = self._registry()["issued"][name]["files"]
        return Path(files["cert"]).read_text()
