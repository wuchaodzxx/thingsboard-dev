"""certmgr — 证书管理与 TB 设备绑定 CLI。

用法示例（在 certmgr/ 目录下，虚拟环境 python 运行）：
  python -m certmgr init-ca
  python -m certmgr issue-server --out ../deploy/certs/server
  python -m certmgr issue-device --name pv-0001
  python -m certmgr sign-csr --name pv-9000 --csr /path/to/vendor.csr
  python -m certmgr verify --name pv-0001
  python -m certmgr list
  python -m certmgr revoke --name pv-0001
  python -m certmgr tb-register --name pv-0001 --type pv
  python -m certmgr tb-revoke --name pv-0001
全局参数 --json 输出机器可读结果（供测试/前端复用）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .pki import PKI

PKG_DIR = Path(__file__).resolve().parent.parent
DEF_SECRETS = Path(os.environ.get("CERTMGR_SECRETS", PKG_DIR / "secrets"))
DEF_SERVER_OUT = Path(os.environ.get("CERTMGR_SERVER_OUT", PKG_DIR.parent / "deploy/certs/server"))


def out(obj, as_json: bool) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if as_json
          else json.dumps(obj, ensure_ascii=False))


def _pki(args) -> PKI:
    return PKI(Path(args.secrets))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="certmgr", description="VPP 证书管理工具")
    ap.add_argument("--secrets", default=str(DEF_SECRETS), help="PKI 根目录（默认 certmgr/secrets）")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init-ca", help="初始化根CA+中间CA")
    p.add_argument("--org", default="VPP-Lab")
    p.add_argument("--root-cn", default="VPP Root CA")
    p.add_argument("--int-cn", default="VPP Intermediate CA 01")
    p.add_argument("--days-root", type=int, default=7300)
    p.add_argument("--days-int", type=int, default=7300)
    p.add_argument("--force", action="store_true", help="覆盖重建（作废全部已签发证书）")

    p = sub.add_parser("issue-server", help="签发 MQTT 服务器证书")
    p.add_argument("--out", default=str(DEF_SERVER_OUT))
    p.add_argument("--name", default="server")
    p.add_argument("--days", type=int, default=7300)
    p.add_argument("--dns", nargs="*", default=["localhost", "tb"])
    p.add_argument("--ip", nargs="*", default=["127.0.0.1"])

    p = sub.add_parser("issue-device", help="签发设备证书（CN=设备名）")
    p.add_argument("--name", required=True)
    p.add_argument("--days", type=int, default=7300)
    p.add_argument("--out", default=None, help="自定义输出目录（默认 secrets/devices/<name>）")

    p = sub.add_parser("sign-csr", help="签发外部 CSR（私钥不出厂家环境）")
    p.add_argument("--name", required=True)
    p.add_argument("--csr", required=True)
    p.add_argument("--days", type=int, default=7300)

    p = sub.add_parser("export-p12", help="导出 PKCS#12（导入测试工具用）")
    p.add_argument("--name", required=True)
    p.add_argument("--password", default="changeit")

    for cmd, help_t in (("verify", "校验证书链"),):
        p = sub.add_parser(cmd, help=help_t)
        p.add_argument("--name", required=True)

    p = sub.add_parser("list", help="列出登记簿")
    p = sub.add_parser("expiry", help="即将到期的证书")
    p.add_argument("--days", type=int, default=30)
    p = sub.add_parser("revoke", help="登记吊销（平台侧须配合 tb-revoke）")
    p.add_argument("--name", required=True)
    p.add_argument("--reason", default="unspecified")

    p = sub.add_parser("tb-register", help="创建 TB 设备并绑定 X.509 凭据")
    p.add_argument("--name", required=True, help="设备名（=证书 CN）")
    p.add_argument("--type", default="default", help="设备类型 pv|pcs|load|wind|default")
    p.add_argument("--profile", default=None, help="设备配置档案（默认=type）")
    p.add_argument("--tb-url", default=os.environ.get("TB_URL", "http://localhost:18080"))
    p.add_argument("--tb-user", default=os.environ.get("TB_USER", "tenant@thingsboard.org"))
    p.add_argument("--tb-pass", default=os.environ.get("TB_PASS", "tenant"))

    p = sub.add_parser("tb-revoke", help="删除 TB 设备 X.509 凭据（吊销落地）")
    p.add_argument("--name", required=True)
    p.add_argument("--tb-url", default=os.environ.get("TB_URL", "http://localhost:18080"))
    p.add_argument("--tb-user", default=os.environ.get("TB_USER", "tenant@thingsboard.org"))
    p.add_argument("--tb-pass", default=os.environ.get("TB_PASS", "tenant"))

    args = ap.parse_args(argv)
    pki = _pki(args)

    if args.cmd == "init-ca":
        out(pki.init_ca(args.org, args.root_cn, args.int_cn,
                        args.days_root, args.days_int, args.force), args.json)
    elif args.cmd == "issue-server":
        out(pki.issue_server_cert(Path(args.out), args.name, args.days,
                                  args.dns, args.ip), args.json)
    elif args.cmd == "issue-device":
        out(pki.issue_device_cert(args.name, args.days,
                                  out_dir=Path(args.out) if args.out else None), args.json)
    elif args.cmd == "sign-csr":
        out(pki.sign_external_csr(Path(args.csr), args.name, args.days), args.json)
    elif args.cmd == "export-p12":
        reg = {c["name"]: c for c in pki.list_certs()}
        if args.name not in reg:
            print(f"不存在：{args.name}", file=sys.stderr)
            return 1
        files = reg[args.name]["files"]
        chain = files.get("fullchain") or files.get("chain")
        from .openssl_tool import export_p12
        dst = Path(files["cert"]).parent / f"{args.name}.p12"
        export_p12(Path(files["cert"]), Path(files["key"]), dst, args.name,
                   args.password, Path(chain) if chain else None)
        out({"p12": str(dst)}, args.json)
    elif args.cmd == "verify":
        out({"name": args.name, "result": pki.verify_device(args.name)}, args.json)
    elif args.cmd == "list":
        out(pki.list_certs(), args.json)
    elif args.cmd == "expiry":
        out(pki.check_expiry(args.days), args.json)
    elif args.cmd == "revoke":
        out(pki.revoke(args.name, args.reason), args.json)
    elif args.cmd == "tb-register":
        from .tb_client import TBClient
        tb = TBClient(args.tb_url, args.tb_user, args.tb_pass)
        tb.login()
        dev = tb.register_device_with_cert(args.name, pki.cert_pem(args.name),
                                           args.type)
        out({"device": dev["name"], "deviceId": dev["id"]["id"],
             "credentialsType": "X509_CERTIFICATE"}, args.json)
    elif args.cmd == "tb-revoke":
        from .tb_client import TBClient
        tb = TBClient(args.tb_url, args.tb_user, args.tb_pass)
        tb.login()
        dev = tb.get_device(args.name)
        if dev is None:
            out({"error": f"TB 中不存在设备 {args.name}"}, args.json)
            return 1
        tb.reset_credentials_to_access_token(dev["id"]["id"])
        out({"device": args.name, "credentialsResetToAccessToken": True,
             "x509Revoked": True}, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
