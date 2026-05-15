#!/usr/bin/env python3
"""
Upload local product images to Alibaba Cloud OSS and generate URL mappings.

Outputs:
- output/image_url_mapping.json
- output/image_url_mapping.csv
- output/image_upload_report.md
"""

from __future__ import annotations

import argparse
import base64
import csv
import email.utils
import hashlib
import hmac
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse, request


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rag_app.config import load_env, safe_text, truncate

IMAGE_DIR = "output/images"
DEFAULT_UPLOAD_PREFIX = "rag/images/"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_SLEEP = 1.5
REPORT_PATH = "output/image_upload_report.md"
MAPPING_JSON_PATH = "output/image_url_mapping.json"
MAPPING_CSV_PATH = "output/image_url_mapping.csv"


def normalize_prefix(prefix: str) -> str:
    prefix = prefix.strip().strip("/")
    return f"{prefix}/" if prefix else ""


def normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    if not endpoint:
        return ""
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    return "https://" + endpoint


def quote_key(key: str) -> str:
    return parse.quote(key, safe="/")


def load_oss_config(require_credentials: bool) -> dict[str, str]:
    load_env(ROOT)
    config = {
        "access_key_id": os.getenv("OSS_ACCESS_KEY_ID", "").strip(),
        "access_key_secret": os.getenv("OSS_ACCESS_KEY_SECRET", "").strip(),
        "endpoint": os.getenv("OSS_ENDPOINT", "").strip(),
        "bucket": os.getenv("OSS_BUCKET", "").strip(),
        "public_base_url": os.getenv("OSS_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        "upload_prefix": normalize_prefix(os.getenv("OSS_UPLOAD_PREFIX", DEFAULT_UPLOAD_PREFIX)),
    }
    required = ["endpoint", "bucket", "public_base_url"]
    if require_credentials:
        required.extend(["access_key_id", "access_key_secret"])
    env_names = {
        "access_key_id": "OSS_ACCESS_KEY_ID",
        "access_key_secret": "OSS_ACCESS_KEY_SECRET",
        "endpoint": "OSS_ENDPOINT",
        "bucket": "OSS_BUCKET",
        "public_base_url": "OSS_PUBLIC_BASE_URL",
    }
    missing = [env_names[key] for key in required if not config[key]]
    if missing:
        raise RuntimeError("缺少 OSS 配置：" + ", ".join(missing) + "。请在 .env 或环境变量中配置。")
    config["endpoint"] = normalize_endpoint(config["endpoint"])
    return config


def scan_images(image_dir: Path) -> list[Path]:
    if not image_dir.exists() or not image_dir.is_dir():
        raise FileNotFoundError(f"本地图片目录不存在：{image_dir}")
    images = [
        path
        for path in image_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    return sorted(images, key=lambda path: path.name)


def to_relative_path(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def build_oss_key(path: Path, upload_prefix: str) -> str:
    return upload_prefix + path.name


def build_image_url(public_base_url: str, oss_key: str) -> str:
    return public_base_url.rstrip("/") + "/" + quote_key(oss_key)


def guess_content_type(path: Path) -> str:
    content_type, _ = mimetypes.guess_type(path.name)
    return content_type or "application/octet-stream"


def build_mapping(images: list[Path], config: dict[str, str]) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    for path in images:
        image_path = to_relative_path(path)
        oss_key = build_oss_key(path, config["upload_prefix"])
        mapping[image_path] = {
            "image_path": image_path,
            "oss_key": oss_key,
            "image_url": build_image_url(config["public_base_url"], oss_key),
        }
    return mapping


def import_oss2():
    try:
        import oss2  # type: ignore
    except Exception:
        return None
    return oss2


class OssClient:
    def __init__(self, config: dict[str, str]):
        self.config = config
        self.oss2 = import_oss2()
        if self.oss2 is not None:
            auth = self.oss2.Auth(config["access_key_id"], config["access_key_secret"])
            self.bucket = self.oss2.Bucket(auth, config["endpoint"], config["bucket"])
        else:
            self.bucket = None

    def object_exists(self, key: str) -> bool:
        if self.oss2 is not None:
            return bool(self.bucket.object_exists(key))
        return self._rest_exists(key)

    def upload_file(self, key: str, path: Path) -> None:
        if self.oss2 is not None:
            self.bucket.put_object_from_file(key, str(path))
            return
        self._rest_put(key, path)

    def _object_url(self, key: str) -> str:
        endpoint = self.config["endpoint"]
        parsed = parse.urlparse(endpoint)
        scheme = parsed.scheme or "https"
        host = parsed.netloc or parsed.path
        bucket = self.config["bucket"]
        if host.startswith(bucket + "."):
            object_host = host
        else:
            object_host = f"{bucket}.{host}"
        return f"{scheme}://{object_host}/{quote_key(key)}"

    def _authorization(self, method: str, key: str, date_value: str, content_type: str = "") -> str:
        canonical_resource = f"/{self.config['bucket']}/{key}"
        string_to_sign = f"{method}\n\n{content_type}\n{date_value}\n{canonical_resource}"
        digest = hmac.new(
            self.config["access_key_secret"].encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")
        return f"OSS {self.config['access_key_id']}:{signature}"

    def _rest_exists(self, key: str) -> bool:
        date_value = email.utils.formatdate(usegmt=True)
        headers = {
            "Date": date_value,
            "Authorization": self._authorization("HEAD", key, date_value),
        }
        req = request.Request(self._object_url(key), method="HEAD", headers=headers)
        try:
            with request.urlopen(req, timeout=30):
                return True
        except urllib_error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise RuntimeError(f"检查 OSS 对象失败 HTTP {exc.code}: {key}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"检查 OSS 对象网络失败：{exc.reason}") from exc

    def _rest_put(self, key: str, path: Path) -> None:
        content_type = guess_content_type(path)
        data = path.read_bytes()
        date_value = email.utils.formatdate(usegmt=True)
        headers = {
            "Date": date_value,
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "Authorization": self._authorization("PUT", key, date_value, content_type),
        }
        req = request.Request(self._object_url(key), data=data, method="PUT", headers=headers)
        try:
            with request.urlopen(req, timeout=120) as response:
                if response.status not in (200, 201):
                    raise RuntimeError(f"上传 OSS 对象失败 HTTP {response.status}: {key}")
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"上传 OSS 对象失败 HTTP {exc.code}: {key}: {body[:600]}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"上传 OSS 对象网络失败：{exc.reason}") from exc


def write_mapping_json(path: Path, mapping: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def write_mapping_csv(path: Path, mapping: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=["image_path", "oss_key", "image_url"])
        writer.writeheader()
        for item in mapping.values():
            writer.writerow(item)


def build_report(summary: dict[str, Any], samples: list[dict[str, str]], failures: list[dict[str, str]]) -> str:
    lines = [
        "# 图片 OSS 上传报告",
        "",
        "## 总览",
        "",
        f"- dry_run：{summary['dry_run']}",
        f"- 扫描图片总数：{summary['total_images']}",
        f"- 计划上传数：{summary['planned_uploads']}",
        f"- 成功上传数：{summary['uploaded_count']}",
        f"- 已存在跳过数：{summary['skipped_existing_count']}",
        f"- 失败数：{summary['failed_count']}",
        f"- OSS bucket：{summary['bucket']}",
        f"- OSS prefix：{summary['upload_prefix']}",
        f"- 映射 JSON：{summary['mapping_json']}",
        f"- 映射 CSV：{summary['mapping_csv']}",
        "",
        "## 前 10 条映射样例",
        "",
        "| image_path | oss_key | image_url |",
        "| --- | --- | --- |",
    ]
    for item in samples:
        lines.append(
            "| "
            + " | ".join(
                [
                    truncate(item["image_path"], 90),
                    truncate(item["oss_key"], 90),
                    truncate(item["image_url"], 120),
                ]
            )
            + " |"
        )
    if not samples:
        lines.append("| 无 |  |  |")

    lines.extend(["", "## 失败明细", "", "| image_path | oss_key | error |", "| --- | --- | --- |"])
    for item in failures:
        lines.append(
            "| "
            + " | ".join(
                [
                    truncate(item.get("image_path", ""), 90),
                    truncate(item.get("oss_key", ""), 90),
                    truncate(item.get("error", ""), 160),
                ]
            )
            + " |"
        )
    if not failures:
        lines.append("| 无 |  |  |")
    return "\n".join(lines) + "\n"


def upload_with_retries(client: OssClient, key: str, path: Path, retries: int, retry_sleep: float) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            client.upload_file(key, path)
            return
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_sleep * attempt)
    raise RuntimeError(str(last_error) if last_error else "未知上传失败")


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_oss_config(require_credentials=not args.dry_run)
    images = scan_images(ROOT / IMAGE_DIR)
    mapping = build_mapping(images, config)

    uploaded_count = 0
    skipped_existing_count = 0
    failures: list[dict[str, str]] = []
    planned_uploads = len(images)
    available_image_paths: set[str] = set(mapping.keys()) if args.dry_run else set()

    if not args.dry_run:
        client = OssClient(config)
        planned_uploads = 0
        for path in images:
            row = mapping[to_relative_path(path)]
            key = row["oss_key"]
            try:
                if client.object_exists(key):
                    skipped_existing_count += 1
                    available_image_paths.add(row["image_path"])
                    continue
                planned_uploads += 1
                upload_with_retries(client, key, path, args.retries, args.retry_sleep)
                uploaded_count += 1
                available_image_paths.add(row["image_path"])
            except Exception as exc:
                failures.append(
                    {
                        "image_path": row["image_path"],
                        "oss_key": key,
                        "error": safe_text(exc).strip(),
                    }
                )

    output_mapping = {image_path: mapping[image_path] for image_path in sorted(available_image_paths)}
    write_mapping_json(ROOT / args.mapping_json, output_mapping)
    write_mapping_csv(ROOT / args.mapping_csv, output_mapping)

    summary = {
        "dry_run": args.dry_run,
        "total_images": len(images),
        "planned_uploads": planned_uploads,
        "uploaded_count": uploaded_count,
        "skipped_existing_count": skipped_existing_count,
        "failed_count": len(failures),
        "bucket": config["bucket"],
        "upload_prefix": config["upload_prefix"],
        "mapping_json": str((ROOT / args.mapping_json).resolve()),
        "mapping_csv": str((ROOT / args.mapping_csv).resolve()),
    }
    samples = list(output_mapping.values())[:10]
    report = build_report(summary, samples, failures)
    report_path = ROOT / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return {**summary, "report": str(report_path.resolve())}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload output/images to OSS and generate URL mappings.")
    parser.add_argument("--dry-run", action="store_true", help="Only build upload plan and mapping files.")
    parser.add_argument("--retries", type=int, default=DEFAULT_MAX_RETRIES, help="Upload retry count.")
    parser.add_argument("--retry-sleep", type=float, default=DEFAULT_RETRY_SLEEP, help="Base retry sleep seconds.")
    parser.add_argument("--mapping-json", default=MAPPING_JSON_PATH, help="Output mapping JSON path.")
    parser.add_argument("--mapping-csv", default=MAPPING_CSV_PATH, help="Output mapping CSV path.")
    parser.add_argument("--report", default=REPORT_PATH, help="Output upload report path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.retries <= 0:
        raise ValueError("--retries 必须大于 0")
    try:
        summary = run(args)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(
        "Upload complete: "
        f"dry_run={summary['dry_run']}, "
        f"total={summary['total_images']}, "
        f"planned={summary['planned_uploads']}, "
        f"uploaded={summary['uploaded_count']}, "
        f"skipped={summary['skipped_existing_count']}, "
        f"failed={summary['failed_count']}, "
        f"report={summary['report']}"
    )
    return 0 if summary["failed_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
