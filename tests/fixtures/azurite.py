# tests/fixtures/azurite.py
"""Azurite blob emulator fixture — migrated from tests/conftest.py."""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest


def _find_azurite_bin() -> str | None:
    """Locate the blob-only Azurite CLI binary."""
    repo_root = Path(__file__).resolve().parents[2]
    local_bin = repo_root / "node_modules" / ".bin" / "azurite-blob"
    if local_bin.exists():
        return str(local_bin)
    return shutil.which("azurite-blob")


def _get_free_port() -> int:
    """Get an available local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(host: str, port: int, timeout_seconds: float = 5.0) -> bool:
    """Wait until a TCP port is accepting connections."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _build_azurite_connection_string(host: str, port: int) -> str:
    """Build Azure Storage connection string for Azurite (blob only)."""
    account_name = "devstoreaccount1"
    account_key = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
    return (
        "DefaultEndpointsProtocol=http;"
        f"AccountName={account_name};"
        f"AccountKey={account_key};"
        f"BlobEndpoint=http://{host}:{port}/{account_name};"
    )


@pytest.fixture(scope="session")
def azurite_blob_service(tmp_path_factory):
    """Start Azurite (blob-only) and provide connection details."""
    pytest.importorskip("azure.storage.blob")

    azurite_bin = _find_azurite_bin()
    if azurite_bin is None:
        pytest.skip("Azurite CLI not found.")

    host = "127.0.0.1"
    port = _get_free_port()
    data_dir = tmp_path_factory.mktemp("azurite")

    cmd = [
        azurite_bin,
        "--silent",
        "--skipApiVersionCheck",
        "--disableProductStyleUrl",
        "--location",
        str(data_dir),
        "--blobHost",
        host,
        "--blobPort",
        str(port),
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parents[2]),
    )

    if not _wait_for_port(host, port):
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
        pytest.skip("Azurite failed to start.")

    connection_string = _build_azurite_connection_string(host, port)
    previous = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    os.environ["AZURE_STORAGE_CONNECTION_STRING"] = connection_string

    try:
        yield {
            "connection_string": connection_string,
            "host": host,
            "port": port,
            "process": process,
        }
    finally:
        if previous is None:
            os.environ.pop("AZURE_STORAGE_CONNECTION_STRING", None)
        else:
            os.environ["AZURE_STORAGE_CONNECTION_STRING"] = previous

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def azurite_blob_container(azurite_blob_service):
    """Create a temporary container in Azurite for blob tests."""
    from azure.storage.blob import BlobServiceClient

    connection_string = azurite_blob_service["connection_string"]
    container_name = f"test-{uuid4().hex}"

    try:
        service_client = BlobServiceClient.from_connection_string(connection_string)
        service_client.create_container(container_name)
    except Exception as exc:
        pytest.skip(f"Azurite connection failed: {exc}")

    try:
        yield {
            "connection_string": connection_string,
            "container": container_name,
        }
    finally:
        with contextlib.suppress(Exception):
            service_client.delete_container(container_name)
