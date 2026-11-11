"""A real background preview through HTTP, on a disposable local broker.

Run INSIDE the production image: `docker run --rm -i IMAGE python - < this.py`.
The unit suite stubs the readers, and the old image smoke test called only the
rasters. Neither exercised the building query that restarted production after
202 Accepted. This owns its database and process and never calls a live broker.
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="geometry-smoke-") as tmp:
        root = Path(tmp)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_hex(16)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CASEBROKER_")}
        env.update(CASEBROKER_DB=str(root / "smoke.sqlite"), CASEBROKER_WRITE_TOKENS=token,
                   CASEBROKER_PUSH="0", PYTHONFAULTHANDLER="1")
        log = root / "broker.log"
        with log.open("w") as output:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "casebroker.app:app",
                 "--host", "127.0.0.1", "--port", str(port)],
                env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10,
                              headers={"Authorization": f"Bearer {token}"}) as client:
                until = time.monotonic() + 30
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError(f"broker exited during startup: {proc.returncode}")
                    try:
                        client.get("/healthz").raise_for_status()
                        break
                    except httpx.HTTPError:
                        if time.monotonic() > until:
                            raise
                        time.sleep(0.2)

                # The reported failure: v2-002c849ccb119c51. The recipe matters:
                # its preview must include CHMv2 and remove crowns in buildings.
                client.post("/v1/cases", json=[{
                    "lat": 23.07504, "lon": 57.70233, "recipe": "cyl-1008/of12-v9",
                    "city_cluster": "geometry-smoke", "lcz": "LCZ3",
                }]).raise_for_status()
                cases = client.get("/v1/cases").json()["cases"]
                assert len(cases) == 1, cases
                url = f"/v1/cases/{cases[0]['case_id']}/footprints?background=1"
                until, previous = time.monotonic() + 180, None
                while True:
                    if proc.poll() is not None:
                        raise RuntimeError(f"preview killed the broker: {proc.returncode}")
                    response = client.get(url)
                    response.raise_for_status()
                    body = response.json()
                    if response.status_code == 200:
                        break
                    assert response.status_code == 202 and body["pending"], body
                    if body["stage"] != previous:
                        previous = body["stage"]
                        print(f"  geometry: {previous}", flush=True)
                    if time.monotonic() > until:
                        raise RuntimeError(f"preview never finished: {body}")
                    client.get("/healthz").raise_for_status()
                    time.sleep(0.5)
                assert body["n"] > 0 and not body.get("buildings_error"), body.get("buildings_error")
                assert body["terrain"]["source"] == "gedtm30", body["terrain"]
                assert body["canopy"]["source"] == "meta-wri-chm-v2", body["canopy"]
                assert client.get(url).json()["cached"] is True
                client.get("/healthz").raise_for_status()
                print(f"  geometry: {body['n']} buildings, terrain, canopy; cached; broker healthy",
                      flush=True)
        except Exception:
            print(log.read_text()[-12000:], file=sys.stderr)
            raise
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    main()
