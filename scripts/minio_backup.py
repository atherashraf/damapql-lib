"""Manifest-based MinIO backup/restore with ZIP archive support."""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from minio.error import S3Error


class MinioBackupManager:
    def __init__(self, minio_manager):
        self.client = minio_manager.client

    @staticmethod
    def _prefix(prefix: str | None) -> str:
        if not prefix:
            return ""
        prefix = prefix.strip("/")
        if not prefix or any(part in (".", "..") for part in prefix.split("/")):
            raise ValueError("Invalid prefix")
        return prefix + "/"

    @staticmethod
    def _sanitize_key(key: str) -> PurePosixPath:
        parts = PurePosixPath(key).parts
        if (not key or key.startswith("/") or "\\" in key or
                any(part in (".", "..") for part in parts) or
                any(":" in part for part in parts)):
            raise ValueError(f"Unsafe object key: {key!r}")
        return PurePosixPath(*parts)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def backup(self, bucket_name: str, destination: str = "./backups",
               prefix: str = "", as_zip: bool = True,
               compress: bool = True) -> dict:
        """
        Back up a bucket or prefix. If as_zip=True, outputs a single .zip file.
        """
        prefix = self._prefix(prefix)
        if not self.client.bucket_exists(bucket_name):
            raise ValueError(f"Bucket does not exist: {bucket_name}")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        dest_dir = Path(destination).expanduser().resolve() / bucket_name
        dest_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "format_version": 2 if as_zip else 1,
            "bucket": bucket_name,
            "prefix": prefix,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "objects": [],
        }

        if as_zip:
            zip_path = dest_dir / f"{stamp}.zip"
            tmp_zip_path = dest_dir / f".{stamp}.zip.tmp"
            compression = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED

            try:
                with zipfile.ZipFile(tmp_zip_path, mode="w", compression=compression) as zf:
                    for obj in self.client.list_objects(bucket_name, prefix=prefix, recursive=True):
                        key = obj.object_name
                        if not key or key.endswith("/"):
                            continue

                        safe_key = str(self._sanitize_key(key))
                        stat = self.client.stat_object(bucket_name, key)
                        response = self.client.get_object(bucket_name, key)

                        digest = hashlib.sha256()
                        bytes_written = 0
                        archive_path = f"objects/{safe_key}"

                        try:
                            with zf.open(archive_path, mode="w") as zf_entry:
                                for chunk in response.stream(1024 * 1024):
                                    digest.update(chunk)
                                    zf_entry.write(chunk)
                                    bytes_written += len(chunk)
                        finally:
                            response.close()
                            response.release_conn()

                        if bytes_written != stat.size:
                            raise IOError(f"Size mismatch during streaming for {key}")

                        manifest["objects"].append({
                            "key": key,
                            "size": bytes_written,
                            "sha256": digest.hexdigest(),
                            "content_type": stat.content_type or "application/octet-stream",
                            "metadata": {k: v for k, v in (stat.metadata or {}).items()
                                         if k.lower().startswith("x-amz-meta-")},
                        })
                        print(f"BACKUP {key} ({bytes_written} bytes)", flush=True)

                    if not manifest["objects"]:
                        raise ValueError("No objects matched the selected bucket/prefix")

                    # Write manifest inside the zip root
                    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
                    zf.writestr("manifest.json", manifest_bytes)

                tmp_zip_path.replace(zip_path)
                print(f"Backup completed: {zip_path}")
                return {"path": str(zip_path), "objects": len(manifest["objects"])}

            except Exception:
                if tmp_zip_path.exists():
                    tmp_zip_path.unlink()
                print(f"Backup failed; cleaned up temporary file: {tmp_zip_path}")
                raise

        else:
            # Directory-based fallback (Legacy format)
            root = dest_dir / stamp
            root.mkdir(parents=True, exist_ok=False)
            objects_root = root / "objects"
            try:
                for obj in self.client.list_objects(bucket_name, prefix=prefix, recursive=True):
                    key = obj.object_name
                    if not key or key.endswith("/"):
                        continue
                    safe_key = self._sanitize_key(key)
                    target = objects_root.joinpath(*safe_key.parts).resolve()
                    if not target.is_relative_to(objects_root.resolve()):
                        raise ValueError(f"Unsafe object key: {key!r}")
                    target.parent.mkdir(parents=True, exist_ok=True)

                    stat = self.client.stat_object(bucket_name, key)
                    self.client.fget_object(bucket_name, key, str(target))
                    size = target.stat().st_size
                    if size != stat.size:
                        raise IOError(f"Size mismatch for {key}")

                    manifest["objects"].append({
                        "key": key,
                        "size": size,
                        "sha256": self._sha256(target),
                        "content_type": stat.content_type or "application/octet-stream",
                        "metadata": {k: v for k, v in (stat.metadata or {}).items()
                                     if k.lower().startswith("x-amz-meta-")},
                    })
                    print(f"BACKUP {key} ({size} bytes)", flush=True)

                if not manifest["objects"]:
                    raise ValueError("No objects matched the selected bucket/prefix")

                (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                print(f"Backup completed: {root}")
                return {"path": str(root), "objects": len(manifest["objects"])}
            except Exception:
                print(f"Backup incomplete; inspect/remove directory: {root}")
                raise

    def restore(self, source: str, bucket_name: str | None = None,
                prefix: str = "", mode: Literal["skip", "overwrite", "dry-run"] = "skip",
                verify: bool = True) -> dict:
        """
        Restores objects from a zip archive or legacy directory backup.
        """
        if mode not in ("skip", "overwrite", "dry-run"):
            raise ValueError("mode must be skip, overwrite, or dry-run")

        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            raise FileNotFoundError(f"Source not found: {source_path}")

        if source_path.is_file() and (source_path.suffix.lower() == ".zip" or zipfile.is_zipfile(source_path)):
            return self._restore_from_zip(source_path, bucket_name, prefix, mode, verify)
        elif source_path.is_dir():
            return self._restore_from_dir(source_path, bucket_name, prefix, mode, verify)
        else:
            raise ValueError(f"Source must be a .zip file or backup directory: {source_path}")

    def _restore_from_zip(self, zip_path: Path, bucket_name: str | None,
                          prefix: str, mode: str, verify: bool) -> dict:
        with zipfile.ZipFile(zip_path, mode="r") as zf:
            try:
                manifest_data = zf.read("manifest.json")
            except KeyError:
                raise ValueError("ZIP archive missing manifest.json")

            manifest = json.loads(manifest_data.decode("utf-8"))
            if manifest.get("format_version") not in (1, 2):
                raise ValueError(f"Unsupported manifest version: {manifest.get('format_version')}")

            bucket = bucket_name or manifest["bucket"]
            selected_prefix = self._prefix(prefix)
            entries = [x for x in manifest["objects"] if x["key"].startswith(selected_prefix)]

            if not entries:
                raise ValueError("No objects matched the restore prefix")

            if mode != "dry-run" and not self.client.bucket_exists(bucket):
                self.client.make_bucket(bucket)

            restored = skipped = 0
            for entry in entries:
                key = entry["key"]
                safe_key = str(self._sanitize_key(key))
                archive_member = f"objects/{safe_key}"

                try:
                    info = zf.getinfo(archive_member)
                except KeyError:
                    raise IOError(f"Missing backup object in zip: {key}")

                if info.file_size != entry["size"]:
                    raise IOError(f"Wrong size for {key}: expected {entry['size']}, got {info.file_size}")

                if mode == "dry-run":
                    # Verify sha256 even in dry-run
                    digest = hashlib.sha256()
                    with zf.open(archive_member, "r") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != entry["sha256"]:
                        raise IOError(f"SHA256 mismatch: {key}")
                    print(f"WOULD RESTORE {bucket}/{key}")
                    continue

                if mode == "skip":
                    try:
                        self.client.stat_object(bucket, key)
                    except S3Error as err:
                        if err.code not in ("NoSuchKey", "NoSuchObject", "NotFound"):
                            raise
                    else:
                        skipped += 1
                        print(f"SKIP {key}")
                        continue

                # Stream from zip into memory buffer or directly if size fits, computing SHA256
                digest = hashlib.sha256()
                payload = io.BytesIO()
                with zf.open(archive_member, "r") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                        payload.write(chunk)

                if digest.hexdigest() != entry["sha256"]:
                    raise IOError(f"SHA256 mismatch: {key}")

                payload.seek(0)
                self.client.put_object(
                    bucket_name=bucket,
                    object_name=key,
                    data=payload,
                    length=entry["size"],
                    content_type=entry.get("content_type") or "application/octet-stream",
                    metadata=entry.get("metadata") or None,
                )

                if verify:
                    stat = self.client.stat_object(bucket, key)
                    if stat.size != entry["size"]:
                        raise IOError(f"Remote size mismatch: {key}")

                restored += 1
                print(f"RESTORED {key}", flush=True)

        result = {"bucket": bucket, "selected": len(entries),
                  "restored": restored, "skipped": skipped, "mode": mode}
        print(result)
        return result

    def _restore_from_dir(self, root: Path, bucket_name: str | None,
                          prefix: str, mode: str, verify: bool) -> dict:
        manifest_file = root / "manifest.json"
        if not manifest_file.exists():
            raise ValueError(f"Directory missing manifest.json: {root}")

        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest.get("format_version") not in (1, 2):
            raise ValueError("Unsupported manifest version")

        bucket = bucket_name or manifest["bucket"]
        selected_prefix = self._prefix(prefix)
        entries = [x for x in manifest["objects"] if x["key"].startswith(selected_prefix)]

        if not entries:
            raise ValueError("No objects matched the restore prefix")

        if mode != "dry-run" and not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

        restored = skipped = 0
        for entry in entries:
            key = entry["key"]
            safe_key = self._sanitize_key(key)
            file = root.joinpath("objects", *safe_key.parts).resolve()

            if not file.is_file() or file.stat().st_size != entry["size"]:
                raise IOError(f"Missing or wrong-size backup object: {key}")
            if self._sha256(file) != entry["sha256"]:
                raise IOError(f"SHA256 mismatch: {key}")

            if mode == "dry-run":
                print(f"WOULD RESTORE {bucket}/{key}")
                continue

            if mode == "skip":
                try:
                    self.client.stat_object(bucket, key)
                except S3Error as err:
                    if err.code not in ("NoSuchKey", "NoSuchObject", "NotFound"):
                        raise
                else:
                    skipped += 1
                    print(f"SKIP {key}")
                    continue

            self.client.fput_object(
                bucket, key, str(file),
                content_type=entry.get("content_type") or "application/octet-stream",
                metadata=entry.get("metadata") or None,
            )
            if verify:
                stat = self.client.stat_object(bucket, key)
                if stat.size != entry["size"]:
                    raise IOError(f"Remote size mismatch: {key}")

            restored += 1
            print(f"RESTORED {key}", flush=True)

        result = {"bucket": bucket, "selected": len(entries),
                  "restored": restored, "skipped": skipped, "mode": mode}
        print(result)
        return result