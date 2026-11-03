"""OpenSSL CLI 封装：密钥/CSR/证书签发/校验。

全部通过临时扩展配置文件下发证书扩展，保证 OpenSSL/LibreSSL 跨版本行为一致。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

OPENSSL = os.environ.get("OPENSSL_BIN", "openssl")


class OpensslError(RuntimeError):
    pass


def run(args: list[str], stdin: str | None = None) -> str:
    p = subprocess.run(
        [OPENSSL, *args], input=stdin, capture_output=True, text=True
    )
    if p.returncode != 0:
        raise OpensslError(
            f"openssl {' '.join(args[:3])}... failed rc={p.returncode}\n{p.stderr.strip()}"
        )
    return p.stdout


def gen_key(out: Path, bits: int = 2048) -> None:
    run(["genrsa", "-out", str(out), str(bits)])


def _write_tmp_conf(text: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".cnf", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


ROOT_CA_SECTION = """\
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:1
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
"""

INTERMEDIATE_CA_SECTION = """\
[v3_ext]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
"""


def self_signed_root_ca(key: Path, out_cert: Path, subject: str, days: int) -> None:
    """自签根 CA 证书。"""
    conf = _write_tmp_conf("[req]\ndistinguished_name=dn\n[dn]\n" + ROOT_CA_SECTION)
    try:
        run([
            "req", "-x509", "-new", "-sha256", "-days", str(days),
            "-key", str(key), "-out", str(out_cert), "-subj", subject,
            "-config", conf, "-extensions", "v3_ca",
        ])
    finally:
        os.unlink(conf)


def make_csr(key: Path, out_csr: Path, subject: str) -> None:
    run(["req", "-new", "-sha256", "-key", str(key), "-out", str(out_csr),
         "-subj", subject])


def sign_csr(
    csr: Path,
    ca_cert: Path,
    ca_key: Path,
    out_cert: Path,
    days: int,
    ext_text: str,
) -> None:
    """用 CA 签发叶子/子 CA 证书（ext_text 含扩展段，段名为 v3_ext）。"""
    ext = _write_tmp_conf(ext_text)
    try:
        run([
            "x509", "-req", "-sha256", "-days", str(days),
            "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
            "-CAcreateserial", "-extfile", ext, "-extensions", "v3_ext",
            "-out", str(out_cert),
        ])
    finally:
        os.unlink(ext)


def intermediate_ca_ext() -> str:
    return INTERMEDIATE_CA_SECTION


def server_cert_ext(dns: list[str], ips: list[str]) -> str:
    lines = [
        "[v3_ext]",
        "basicConstraints = CA:FALSE",
        "keyUsage = critical, digitalSignature, keyEncipherment",
        "extendedKeyUsage = serverAuth",
        "subjectKeyIdentifier = hash",
        "authorityKeyIdentifier = keyid:always,issuer",
        "subjectAltName = @alt_names",
        "",
        "[alt_names]",
    ]
    for i, d in enumerate(dns or ["localhost"], 1):
        lines.append(f"DNS.{i} = {d}")
    for i, ip in enumerate(ips or ["127.0.0.1"], 1):
        lines.append(f"IP.{i} = {ip}")
    return "\n".join(lines)


def device_cert_ext(cn: str) -> str:
    return (
        "[v3_ext]\n"
        "basicConstraints = CA:FALSE\n"
        "keyUsage = critical, digitalSignature\n"
        "extendedKeyUsage = clientAuth\n"
        "subjectKeyIdentifier = hash\n"
        "authorityKeyIdentifier = keyid:always,issuer\n"
        f"# CN={cn}\n"
    )


def verify(cert: Path, cafile: Path, *untrusted: Path) -> str:
    args = ["verify", "-CAfile", str(cafile)]
    for u in untrusted:
        args += ["-untrusted", str(u)]
    args.append(str(cert))
    return run(args).strip()


def info(cert: Path) -> dict:
    out = run([
        "x509", "-in", str(cert), "-noout",
        "-subject", "-issuer", "-serial", "-dates",
    ])
    d: dict = {}
    for line in out.strip().splitlines():
        k, _, v = line.partition("=")
        d[k.strip()] = v.strip()
    # 解析 RFC2253 subject 提取 CN
    d["cn"] = ""
    for part in d.get("subject", "").split("/"):
        if part.startswith("CN="):
            d["cn"] = part[3:]
    return d


def enddate(cert: Path) -> str:
    return run(["x509", "-in", str(cert), "-noout", "-enddate"]).strip()


def serial(cert: Path) -> str:
    return run(["x509", "-in", str(cert), "-noout", "-serial"]).strip().split("=", 1)[1]


def export_p12(cert: Path, key: Path, out: Path, name: str, passphrase: str, chain: Path | None) -> None:
    args = ["pkcs12", "-export", "-out", str(out), "-inkey", str(key),
            "-in", str(cert), "-name", name, "-passout", f"pass:{passphrase}"]
    if chain:
        args += ["-certfile", str(chain)]
    run(args)
