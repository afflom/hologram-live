"""Infrastructure regression tests, not PrismPM/product acceptance oracles."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("prism_sdk", Path(__file__).with_name("prism-sdk.py"))
SDK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SDK)
DIGEST = "sha256:" + "a" * 64
IMAGE = "ghcr.io/example/sdk@" + DIGEST


class LockTests(unittest.TestCase):
    def lock(self, **overrides):
        value = {"schema": "prismpm/sdk-lock/1", "sdk_image": IMAGE,
                 "sdk_version": "0.3.0", "inventory": [
                     {"id": "sdk-manifest", "digest": DIGEST, "version": "0.3.0"}]}
        value.update(overrides)
        return value

    def read(self, value):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prismpm.lock").write_text(json.dumps(value))
            return SDK.locked_image(root)

    def test_immutable_lock(self):
        self.assertEqual(self.read(self.lock()), IMAGE)

    def test_mutable_or_malformed_image(self):
        for image in ("sdk:latest", "sdk@sha256:abc", "sdk@" + DIGEST + "\n", "", "-sdk@" + DIGEST):
            with self.subTest(image=image), self.assertRaises(ValueError):
                self.read(self.lock(sdk_image=image))

    def test_missing_duplicate_or_mismatched_inventory(self):
        record = self.lock()["inventory"][0]
        for inventory in ([], [record, record], [{**record, "digest": "sha256:" + "b" * 64}],
                          [{**record, "version": "other"}]):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                self.read(self.lock(inventory=inventory))

    def test_unknown_lock_schema(self):
        with self.assertRaises(ValueError):
            self.read(self.lock(schema="unknown"))

    def test_repository_lock(self):
        image = SDK.locked_image(Path(__file__).resolve().parent.parent)
        self.assertTrue(image.startswith("ghcr.io/uor-foundation/prismpm-sdk-candidate@sha256:"))

    def test_devcontainer_uses_same_sdk_and_bounded_build_storage(self):
        root = Path(__file__).resolve().parent.parent
        config = json.loads((root / ".devcontainer/devcontainer.json").read_text())
        self.assertEqual(config["image"], SDK.locked_image(root))
        self.assertEqual(config["containerUser"], "vscode")
        self.assertEqual(config["containerEnv"]["CARGO_TARGET_DIR"], "/tmp/hologram-target")
        self.assertIn("--tmpfs=/tmp:rw,exec,nosuid,nodev,size=2g,mode=1777", config["runArgs"])
        self.assertNotIn("/var/run/docker.sock", json.dumps(config))


class BudgetTests(unittest.TestCase):
    def test_timeout_bounds(self):
        self.assertEqual(SDK.bounded_timeout("1"), 1)
        self.assertEqual(SDK.bounded_timeout("3600"), 3600)
        for value in ("0", "-1", "3601"):
            with self.subTest(value=value), self.assertRaises(SDK.argparse.ArgumentTypeError):
                SDK.bounded_timeout(value)

    def test_reserve_boundary(self):
        SDK.require_capacity(10 * SDK.GIB, 5 * SDK.GIB, 2)

    def test_low_disk(self):
        with self.assertRaisesRegex(ValueError, "disk reserve"):
            SDK.require_capacity(10 * SDK.GIB - 1, 10 * SDK.GIB, 2)

    def test_low_memory(self):
        with self.assertRaisesRegex(ValueError, "RAM"):
            SDK.require_capacity(20 * SDK.GIB, 5 * SDK.GIB - 1, 2)


class CommandTests(unittest.TestCase):
    def command(self, args=None):
        return SDK.docker_command(IMAGE, "hologram-prism-sdk-test", 2,
                                  args or ["prismpm", "--version"])

    def test_no_pull_network_host_mount_or_persistent_volume(self):
        command = self.command()
        for required in ("--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                         "--security-opt=no-new-privileges", "--log-driver=none", "--rm"):
            self.assertIn(required, command)
        for forbidden in ("--mount", "--volume", "-v", "--privileged"):
            self.assertNotIn(forbidden, command)
        self.assertNotIn("/var/run/docker.sock", " ".join(command))

    def test_bounded_resources_without_swap(self):
        command = self.command()
        for option in ("--memory=3g", "--memory-swap=3g", "--cpus=2", "--pids-limit=256"):
            self.assertIn(option, command)
        self.assertIn("/work:rw,exec,nosuid,nodev,uid=1000,gid=1000,size=2g", command)

    def test_arguments_remain_literal(self):
        args = ["printf", "%s", "$(touch /escaped)", "; exit 0", "space here"]
        command = self.command(args)
        self.assertEqual(command[-len(args):], args)
        self.assertIn('tar -xf -; mkdir .tmp; exec "$@"', command)

    def test_empty_command_rejected(self):
        with self.assertRaises(ValueError):
            SDK.docker_command(IMAGE, "test", 2, [])


if __name__ == "__main__":
    unittest.main()
