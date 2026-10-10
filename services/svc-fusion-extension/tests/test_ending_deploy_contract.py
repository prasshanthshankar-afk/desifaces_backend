"""Exercise deployment preflight using actual Docker/Compose JSON shapes."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).parents[3] / "scripts/ops/deploy-single-person-ending-dev.sh"


class DeploymentContractTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.names = ["svc-fusion-extension", "fusion-extension-worker", "longform-stitch-worker"]
        containers = ["df-svc-fusion-extension", "df-svc-fusion-extension-worker", "df-svc-fusion-extension-stitch-worker"]
        services = {}
        for container, name in zip(containers, self.names):
            services[name] = {"environment": {"PORT": "8006"}, "networks": {"runtime": {}},
                              "command": ["python", "-m", "worker"], "restart": "unless-stopped"}
            live = {"Image": "sha256:baseline", "Config": {"Env": ["PORT=8006"],
                    "Cmd": ["python", "-m", "worker"], "Entrypoint": None,
                    "Labels": {"com.docker.compose.project": "desifaces", "com.docker.compose.service": name}},
                    "NetworkSettings": {"Networks": {"df-v3-net": {}}}, "Mounts": [],
                    "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "PortBindings": {}}}
            (self.root / (container + ".inspect.json")).write_text(json.dumps([live]))
        self.config = {"services": services, "networks": {"runtime": {"name": "df-v3-net"}}}
        self.write_config()

    def write_config(self):
        (self.root / "compose-before.json").write_text(json.dumps(self.config))

    def run_block(self, marker):
        blocks = re.findall(r"<<'PY'\n(.*?)\nPY", SCRIPT.read_text(), re.S)
        block = next(b for b in blocks if marker in b)
        return subprocess.run([sys.executable, "-", str(self.root), "a" * 40],
                              input=block, text=True, capture_output=True)

    def test_worker_service_names_come_from_labels(self):
        result = self.run_block("LIVE_CONFIGURATION_COMPARISON=PASS")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_block("candidate, rollback")
        self.assertEqual(result.returncode, 0, result.stderr)
        candidate = json.loads((self.root / "candidate.json").read_text())
        self.assertEqual(set(candidate["services"]), set(self.names))

    def test_configuration_drift_stops_before_deployment(self):
        self.config["services"][self.names[1]]["environment"]["PORT"] = "wrong"
        self.write_config()
        result = self.run_block("LIVE_CONFIGURATION_COMPARISON=PASS")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("environment drift", result.stderr)

    def test_all_profiles_are_rendered_and_stop_uses_resolved_names(self):
        source = SCRIPT.read_text()
        self.assertIn("docker compose --profile '*'", source)
        self.assertIn('stop "${SERVICES[1]}" "${SERVICES[2]}"', source)
        self.assertNotIn("stop svc-fusion-extension-worker", source)


if __name__ == "__main__":
    unittest.main()
