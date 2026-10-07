import functools
import hashlib
import importlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import xnat
from click.testing import CliRunner

from pydra2app.xnat.cli.deploy import deploy_pipelines
from pydra2app.xnat.reconcile import (
    ReconciliationError,
    load_catalogue,
    load_config,
    reconcile_commands,
)

IMAGE = "ghcr.io/australian-imaging-service/example@sha256:" + "a" * 64


class Response:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class FakeXnat:
    """Models the XNAT behaviour the reconciler relies on: enablement is held per
    wrapper id, and updating a command keeps the ids of wrappers matched by name."""

    def __init__(self, commands=(), failing_paths=(), enabled=()):
        self.commands = [dict(command) for command in commands]
        self.failing_paths = set(failing_paths)
        self.enabled = set(enabled)  # (command id, wrapper id)
        self.next_wrapper_id = 100
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def _wrappers(self, document, current=()):
        ids = {wrapper["name"]: wrapper["id"] for wrapper in current}
        wrappers = []
        for wrapper in document.get("xnat", []):
            if wrapper["name"] not in ids:
                self.next_wrapper_id += 1
            wrappers.append(
                {**wrapper, "id": ids.get(wrapper["name"], self.next_wrapper_id)}
            )
        return wrappers

    def _wrapper_key(self, path):
        # /xapi/commands/{command id}/wrappers/{wrapper name}/enabled
        _, _, _, command_id, _, wrapper_name, _ = path.split("/")
        command = next(c for c in self.commands if c["id"] == int(command_id))
        wrapper = next(w for w in command["xnat"] if w["name"] == wrapper_name)
        return command["id"], wrapper["id"]

    def get(self, path):
        self.calls.append(("get", path, None))
        if path == "/xapi/commands":
            return Response([dict(command) for command in self.commands])
        return Response(self._wrapper_key(path) in self.enabled)

    def put(self, path):
        self.calls.append(("put", path, None))
        self.enabled.add(self._wrapper_key(path))

    def post(self, path, json):
        self.calls.append(("post", path, json))
        if path in self.failing_paths:
            response = SimpleNamespace(url=path, status_code=500, text="failed")
            raise xnat.exceptions.XNATResponseError("XNAT request failed", response)
        if path == "/xapi/commands":
            command_id = len(self.commands) + 1
            self.commands.append(
                {**json, "id": command_id, "xnat": self._wrappers(json)}
            )
        else:
            command_id = int(path.rsplit("/", 1)[1])
            current = next(c for c in self.commands if c["id"] == command_id)
            wrappers = self._wrappers(json, current.get("xnat", []))
            current.clear()
            current.update(json, id=command_id, xnat=wrappers)
        return Response(command_id)


def command_bytes(name="pipeline.example", image=IMAGE):
    return json.dumps(
        {"name": name, "image": image, "xnat": [{"name": "example"}]}
    ).encode()


def write_catalogue(tmp_path, pipelines, assets):
    catalogue = {
        "schema_version": "1.0",
        "release": {"tag": "v1.0"},
        "source": {"repository": "owner/repository", "commit": "b" * 40},
        "pipelines": [],
    }
    downloads = {}
    for pipeline_id, command_names in pipelines:
        commands = []
        for name in command_names:
            content = assets.get((pipeline_id, name)) or assets[name]
            url = (
                "https://github.com/example/releases/download/v1/"
                f"{pipeline_id}.{name}.json"
            )
            downloads[url] = content
            commands.append(
                {
                    "name": name,
                    "path": f"command-{'c' * 64}.json",
                    "url": url,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        catalogue["pipelines"].append(
            {
                "id": pipeline_id,
                "spec": pipeline_id,
                "version": "1.0",
                "image_tag": "ghcr.io/australian-imaging-service/example:1.0",
                "image": IMAGE,
                "commands": commands,
            }
        )
    path = tmp_path / "pipeline-release.json"
    path.write_text(json.dumps(catalogue))
    return path, downloads


def load(path, downloads):
    return load_catalogue(path, download=downloads.__getitem__)


def test_install_then_second_run_is_unchanged_and_leaves_other_commands(tmp_path):
    path, downloads = write_catalogue(
        tmp_path, [("pipeline", ["example"])], {"example": command_bytes()}
    )
    # Duplicates among commands the catalogue doesn't manage must not block it
    unmanaged = {"name": "not-managed", "image": "example@sha256:old", "xnat": []}
    xlogin = FakeXnat([{**unmanaged, "id": 10}, {**unmanaged, "id": 11}])

    first = reconcile_commands(xlogin, load(path, downloads))
    second = reconcile_commands(xlogin, load(path, downloads))

    assert first[0].status == "installed"
    assert second[0].status == "unchanged"
    assert [call[0:2] for call in xlogin.calls].count(("post", "/xapi/commands")) == 1
    assert any(command["name"] == "pipeline.example" for command in xlogin.commands)
    assert any(command["name"] == "not-managed" for command in xlogin.commands)
    assert not xlogin.enabled  # installed disabled


def test_update_uses_supported_endpoint_and_preserves_enablement(tmp_path):
    path, downloads = write_catalogue(
        tmp_path, [("pipeline", ["example"])], {"example": command_bytes()}
    )
    xlogin = FakeXnat(
        [
            {
                "id": 42,
                "name": "pipeline.example",
                "image": "ghcr.io/example@sha256:" + "b" * 64,
                "xnat": [{"id": 7, "name": "example"}],
            }
        ],
        enabled={(42, 7)},
    )

    results = reconcile_commands(xlogin, load(path, downloads))

    assert results[0].status == "updated"
    assert ("post", "/xapi/commands/42") in [call[0:2] for call in xlogin.calls]
    assert xlogin.commands[0]["xnat"][0]["id"] == 7
    assert (42, 7) in xlogin.enabled
    assert not [call for call in xlogin.calls if call[0] == "put"]


def test_checksum_mismatch_fails_before_xnat_is_touched(tmp_path):
    path, downloads = write_catalogue(
        tmp_path, [("pipeline", ["example"])], {"example": command_bytes()}
    )
    downloads[next(iter(downloads))] = b"tampered"
    xlogin = FakeXnat()

    deploy_module = importlib.import_module("pydra2app.xnat.cli.deploy")
    loader = functools.partial(load_catalogue, download=downloads.__getitem__)
    with patch.object(deploy_module, "load_catalogue", loader), patch.object(
        deploy_module.xnat, "connect", return_value=xlogin
    ) as connect:
        result = CliRunner().invoke(
            deploy_pipelines,
            [str(path), "--server", "https://xnat.example", "--user", "u"],
            env={"XNAT_PASS": "p"},
        )

    assert result.exit_code != 0
    assert "Checksum mismatch" in result.output
    connect.assert_not_called()


@pytest.mark.parametrize(
    "content, message",
    [
        (b"{", "not valid JSON"),
        (command_bytes(name="pipeline.other"), "does not match catalogue name"),
        (command_bytes(image="ghcr.io/example@sha256:" + "b" * 64), "does not match"),
    ],
)
def test_invalid_command_asset_fails_clearly(tmp_path, content, message):
    path, downloads = write_catalogue(
        tmp_path, [("pipeline", ["example"])], {"example": content}
    )

    with pytest.raises(ReconciliationError, match=message):
        load(path, downloads)


def test_malformed_catalogue_fails_clearly(tmp_path):
    path = tmp_path / "pipeline-release.json"
    path.write_text('{"schema_version": "1.0", "pipelines": {}}')

    with pytest.raises(ReconciliationError, match="pipelines must be a JSON array"):
        load_catalogue(path)


def test_duplicate_desired_and_installed_names_fail(tmp_path):
    content = command_bytes()
    path, downloads = write_catalogue(
        tmp_path,
        [("first", ["example"]), ("second", ["example"])],
        {"example": content},
    )
    with pytest.raises(ReconciliationError, match="Duplicate desired command"):
        load(path, downloads)

    path, downloads = write_catalogue(
        tmp_path, [("pipeline", ["example"])], {"example": content}
    )
    desired = load(path, downloads)
    xlogin = FakeXnat(
        [
            {"id": 1, "name": "pipeline.example", "image": IMAGE},
            {"id": 2, "name": "pipeline.example", "image": IMAGE},
        ]
    )
    results = reconcile_commands(xlogin, desired)
    assert results[0].status == "failed"
    assert "multiple installed commands" in results[0].error
    assert not [call for call in xlogin.calls if call[0] != "get"]


def test_pipelines_sharing_a_catalogue_command_name(tmp_path):
    path, downloads = write_catalogue(
        tmp_path,
        [("first", ["example"]), ("second", ["example"])],
        {
            ("first", "example"): command_bytes(name="first.example"),
            ("second", "example"): command_bytes(name="second.example"),
        },
    )
    xlogin = FakeXnat()

    results = reconcile_commands(xlogin, load(path, downloads))

    assert [result.status for result in results] == ["installed", "installed"]
    assert sorted(c["name"] for c in xlogin.commands) == [
        "first.example",
        "second.example",
    ]


def test_api_failure_is_reported_and_cli_exits_nonzero(tmp_path):
    path, downloads = write_catalogue(
        tmp_path, [("pipeline", ["example"])], {"example": command_bytes()}
    )
    desired = load(path, downloads)
    xlogin = FakeXnat(failing_paths={"/xapi/commands"})
    results = reconcile_commands(xlogin, desired)
    assert results[0].status == "failed"

    deploy_module = importlib.import_module("pydra2app.xnat.cli.deploy")
    with patch.object(
        deploy_module, "load_catalogue", return_value=desired
    ), patch.object(deploy_module.xnat, "connect", return_value=xlogin):
        result = CliRunner().invoke(
            deploy_pipelines,
            [
                "--server",
                "https://xnat.example",
                "--user",
                "user",
                "--password",
                "password",
            ],
            env={"PIPELINE_CATALOGUE_URL": str(path)},
        )

    assert result.exit_code == 1
    assert result.output.startswith("pipeline: failed")


@pytest.mark.parametrize(
    "content, message",
    [
        ("include: [", "Could not load reconciler config"),
        ("- mri.*", "config must be a YAML mapping"),
        ("includes: [mri.*]", "Unknown config keys: 'includes'"),
        ("include: mri.*", "config.include must be a non-empty list"),
        ("include: []", "config.include must be a non-empty list"),
        ("exclude: ['']", "config.exclude must be a non-empty list"),
        ("enablement: yes", "config.enablement must be"),
    ],
)
def test_invalid_config_fails_clearly(tmp_path, content, message):
    path = tmp_path / "config.yaml"
    path.write_text(content)

    with pytest.raises(ReconciliationError, match=message):
        load_config(path)


def test_config_selects_pipelines_and_leaves_excluded_ones_alone(tmp_path, caplog):
    ids = ["mri.neuro.bids", "mri.neuro.fs", "pet.suv"]
    path, downloads = write_catalogue(
        tmp_path,
        [(pipeline_id, ["example"]) for pipeline_id in ids],
        {(i, "example"): command_bytes(name=f"{i}.example") for i in ids},
    )
    # Only the selected pipeline's command can be downloaded
    downloads = {url: c for url, c in downloads.items() if "/mri.neuro.fs." in url}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "include: [mri.*, ct.*]\nexclude: [mri.neuro.bids]\nenablement: auto\n"
    )
    config = load_config(config_path)
    # An excluded command that is installed but outdated and disabled
    xlogin = FakeXnat(
        [
            {
                "id": 1,
                "name": "mri.neuro.bids.example",
                "image": "ghcr.io/example@sha256:" + "b" * 64,
                "xnat": [{"id": 7, "name": "example"}],
            }
        ]
    )

    with caplog.at_level(logging.WARNING):
        desired = load_catalogue(path, download=downloads.__getitem__, config=config)
    results = reconcile_commands(xlogin, desired, auto_enable=True)

    assert config.enablement == "auto"
    assert [result.pipeline for result in results] == ["mri.neuro.fs"]
    assert "'ct.*' matches no pipeline" in caplog.text
    assert not [call for call in xlogin.calls if "/xapi/commands/1" in call[1]]


def test_auto_enablement_enables_new_and_admin_disabled_commands(tmp_path):
    path, downloads = write_catalogue(
        tmp_path, [("pipeline", ["example"])], {"example": command_bytes()}
    )
    desired = load(path, downloads)
    xlogin = FakeXnat()

    first = reconcile_commands(xlogin, desired, auto_enable=True)
    xlogin.enabled.clear()  # an administrator disables the command
    second = reconcile_commands(xlogin, desired, auto_enable=True)
    third = reconcile_commands(xlogin, desired, auto_enable=True)

    assert [(r[0].status, r[0].enabled) for r in (first, second, third)] == [
        ("installed", 1),
        ("unchanged", 1),
        ("unchanged", 0),
    ]
    assert [call[1] for call in xlogin.calls if call[0] == "put"] == [
        "/xapi/commands/1/wrappers/example/enabled"
    ] * 2
    assert xlogin.enabled == {(1, 101)}


def test_cli_reads_config_from_environment_before_connecting(tmp_path):
    path, downloads = write_catalogue(
        tmp_path, [("pipeline", ["example"])], {"example": command_bytes()}
    )
    config_path = tmp_path / "config.yaml"
    xlogin = FakeXnat()
    deploy_module = importlib.import_module("pydra2app.xnat.cli.deploy")
    loader = functools.partial(load_catalogue, download=downloads.__getitem__)

    def invoke(config):
        config_path.write_text(config)
        with patch.object(deploy_module, "load_catalogue", loader), patch.object(
            deploy_module.xnat, "connect", return_value=xlogin
        ) as connect:
            result = CliRunner().invoke(
                deploy_pipelines,
                [str(path), "--server", "https://xnat.example", "--user", "u"],
                env={"XNAT_PASS": "p", "PIPELINE_RECONCILER_CONFIG": str(config_path)},
            )
        return result, connect.called

    invalid, connected = invoke("enablement: manual\n")
    assert invalid.exit_code != 0
    assert "config.enablement must be" in invalid.output
    assert not connected

    valid, _ = invoke("enablement: auto\n")
    assert valid.exit_code == 0
    assert (
        "pipeline: installed (installed=1, updated=0, unchanged=0, enabled=1)"
        in valid.output
    )
