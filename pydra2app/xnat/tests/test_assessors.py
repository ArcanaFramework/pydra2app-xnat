import csv
import json as json_module
import os
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

import pytest
import xnat
import xnat4tests
from fileformats.text import TextFile
from fileformats.field import Decimal
from frametree.axes.medimage import MedImage
from frametree.xnat.cs import XnatViaCS
from pydra.compose import python
from pydra.utils.hash import hash_single
from xnat.search import Query, SearchField

from pydra2app.xnat import XnatApp
from pydra2app.xnat.assessor_runtime import execute_with_assessor, finalise_assessor
from pydra2app.xnat.assessor_store import AssessorOutputStore
from pydra2app.xnat.assessors import (
    METADATA_KEY,
    PROVENANCE_FIELDS,
    AssessorError,
    _digest,
    assessor_definition,
    ensure_assessor,
    form_builder,
    output_resource_name,
    prepare_assessor_command,
    validate_definition,
    xsi_type,
)
from pydra2app.xnat.reconcile import (
    _DesiredCommand,
    _DesiredPipeline,
    reconcile_commands,
)
from pydra2app.xnat.tests.test_make_command_json import image_spec
from pydra2app.xnat.tests.test_reconcile import FakeXnat


@python.define(outputs=["out_file", "score", "passed"])
def AssessorTask(
    in_file: TextFile, scale: float = 1.0, fail: bool = False
) -> tuple[TextFile, float, bool]:
    """Produce a file and searchable scalar outputs.

    Parameters
    ----------
    in_file : TextFile
        Input text.
    scale : float
        Score multiplier.
    fail : bool
        Fail before producing outputs.

    Returns
    -------
    out_file : TextFile
        A copy of the input.
    score : float
        Length of the input multiplied by scale.
    passed : bool
        Whether the input is nonempty.
    """
    if fail:
        raise RuntimeError("Deliberate pipeline failure")
    contents = Path(in_file).read_text()
    output = Path("result.txt").absolute()
    output.write_text(contents)
    return output, len(contents) * scale, bool(contents)


def make_app(version="1.0", *, image_name="assessor-test", **command_kwargs):
    return XnatApp.load(
        image_spec(
            {
                "task": AssessorTask,
                "operates_on": "medimage/session",
                "assessor_outputs": True,
                "sinks": ["out_file", "score", "passed"],
                **command_kwargs,
            },
            version=version,
        ),
        name=image_name,
        org="test",
    )


def response(value=None, status=200, text=""):
    return SimpleNamespace(json=lambda: value, status_code=status, text=text)


class AssessorXnat(FakeXnat):
    def __init__(self, version="1.10.0"):
        super().__init__()
        self.version = version
        self.schemas = []
        self.forms = []

    def get(self, path, **kwargs):
        if path == "/xapi/siteConfig/buildInfo":
            return response({"version": self.version})
        if path == "/xapi/dbschemas":
            return response(self.schemas)
        if path == "/xapi/customforms":
            return response(self.forms)
        if path.startswith("/xapi/datatypes/"):
            return response(status=404)
        return super().get(path)

    def post(self, path, json=None, query=None, **kwargs):
        if path == "/xapi/datatypes/create":
            self.calls.append(("post", path, query))
            self.schemas.append(
                {
                    "name": f"{query['prefix']}:{query['complexType']}",
                    "content": (
                        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
                        f'<xs:element name="{query["name"]}"/>'
                        "<xs:complexType><xs:complexContent>"
                        f'<xs:extension base="xnat:{query["extends"]}"/>'
                        "</xs:complexContent></xs:complexType></xs:schema>"
                    ),
                    "enabled": True,
                }
            )
            return response(status=201)
        return super().post(path, json)

    def put(self, path, json=None, **kwargs):
        if path == "/xapi/customforms/save":
            self.calls.append(("put", path, json))
            form_uuid = f"form-{len(self.forms)}"
            self.forms.append(
                {
                    "path": "datatype/"
                    + json["submission"]["data"]["xnatDatatype"]["value"],
                    "contents": json_module.dumps(json["builder"]),
                    "scope": "Site",
                    "appliesToList": [{"entityId": "Site", "status": "enabled"}],
                    "formUUID": form_uuid,
                }
            )
            return response(status=201, text=form_uuid)
        return super().put(path)


def test_generated_scalar_fields_and_stable_pipeline_identity():
    command = make_app().command("test-command")
    definition = assessor_definition(command)
    upgraded = assessor_definition(make_app("2.0").command("test-command"))
    assert definition == upgraded
    assert [(field["label"], field["type"]) for field in definition["fields"]] == [
        ("score", "number"),
        ("passed", "checkbox"),
    ]
    document = command.make_json()
    assert "--assessor-outputs" in document["command-line"]
    assert {"out_file", "score", "passed"} <= {
        item["name"] for item in document["inputs"]
    }
    assert document["command-metadata"][METADATA_KEY] == definition
    assert document["xnat"][0]["output-handlers"] == []


@pytest.mark.parametrize("image_name", ["mriqc", "123-tool", "a" * 100])
def test_readable_type_names_preserve_backend_identity(image_name):
    definition = assessor_definition(
        make_app(image_name=image_name).command("test-command")
    )
    validate_definition(definition)
    identity = _digest([definition["pipeline"], "session"])
    assert definition["prefix"] == "p" + identity[:5]
    assert definition["complexType"] == "pipeline" + identity[:12] + "Data"
    assert definition["name"] == "Pipeline" + identity[:12]
    assert identity[:12] not in definition["singular"]
    assert identity[:12] not in definition["plural"]
    if image_name == "mriqc":
        assert definition["singular"] == "Mriqc test command"


def test_form_presents_results_before_readable_provenance_fields():
    command = make_app(
        sinks={
            "out_file": {},
            "mean_QC_score": {"field": "score", "help": "Mean quality score."},
            "passed": {},
        }
    ).command("test-command")
    definition = assessor_definition(command)
    builder = form_builder(definition)
    assert builder["title"].startswith("assessor-test / test-command results (")
    components = builder["components"]
    assert [field["label"] for field in components] == [
        "mean QC score",
        "passed",
        "Pipeline",
        "Pipeline version",
        "Container image",
        "Launch parameters",
        "Input URIs",
        "Workflow ID",
    ]
    assert components[0]["key"] == definition["fields"][0]["key"]
    assert components[0]["description"] == "Mean quality score."
    assert all(field["input"] and field["tableView"] for field in components)


@pytest.mark.parametrize(
    "frequency,base,context,external",
    [
        ("session", "imageAssessorData", "xnat:imageSessionData", "SESSION"),
        ("subject", "subjectAssessorData", "xnat:subjectData", "SUBJECT"),
        ("dataset", "abstractProjectAsset", "xnat:projectData", "PROJECT"),
    ],
)
def test_frequency_definition_and_wrapper(frequency, base, context, external):
    command = make_app(operates_on="medimage/" + frequency).command("test-command")
    document = command.make_json()
    assert document["command-metadata"][METADATA_KEY]["extends"] == base
    wrapper = document["xnat"][0]
    assert wrapper["contexts"] == [context]
    assert wrapper["external-inputs"][0]["name"] == external
    if frequency == "dataset":
        assert "--ids" not in document["command-line"]
    else:
        assert "--ids" in document["command-line"]


@pytest.mark.parametrize("enabled", [True, False])
def test_image_store_configuration_is_opt_in(tmp_path, enabled):
    app = make_app(assessor_outputs=enabled)
    app.save_store_config(
        SimpleNamespace(run=lambda **kw: None, copy=lambda **kw: None), tmp_path
    )
    contents = (tmp_path / "stores.yaml").read_text()
    assert ("AssessorOutputStore" in contents) is enabled
    assert ("XnatViaCS" in contents) is not enabled


def test_numeric_outputs_are_hashable_and_stage_as_json_numbers(tmp_path):
    store = AssessorOutputStore(
        server="https://unused.example",
        user=None,
        password=None,
        cache_dir=tmp_path / "cache",
        assessor_staging_dir=tmp_path,
    )
    assert hash_single(Decimal("2.5"), {}) != hash_single(Decimal("3.5"), {})
    entry = SimpleNamespace(path="score@", is_derivative=True, datatype=Decimal)
    store.upload_value(Decimal("2.5").value, entry)
    record = json_module.loads(next((tmp_path / "outputs").glob("*.json")).read_text())
    assert record["value"] == 2.5


def test_legacy_outputs_do_not_prevent_assessor_execution(tmp_path):
    store = AssessorOutputStore(
        server="https://unused.example",
        user=None,
        password=None,
        cache_dir=tmp_path / "cache",
        assessor_staging_dir=tmp_path,
        assessor_output_paths=("result",),
    )
    row = SimpleNamespace(
        frequency=MedImage.session,
        entries_dict={
            ("result@", None): SimpleNamespace(path="result@"),
            ("source@", None): SimpleNamespace(path="source@"),
        },
    )
    with patch.object(XnatViaCS, "populate_row"):
        store.populate_row(row)
    assert list(row.entries_dict) == [("source@", None)]


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"internal_upload": True}, "internal_upload"),
        ({"operates_on": "medimage/group"}, "frequency"),
    ],
)
def test_unsupported_output_modes_fail_before_generating_a_command(kwargs, message):
    with pytest.raises(AssessorError, match=message):
        make_app(**kwargs).command("test-command").make_json()


def test_registration_and_reconciliation_are_idempotent():
    document = make_app().command("test-command").make_json()
    server = AssessorXnat()
    desired = [
        _DesiredPipeline(
            "pipeline",
            (_DesiredCommand(document["name"], document["image"], document),),
        )
    ]
    assert reconcile_commands(server, desired)[0].status == "installed"
    assert reconcile_commands(server, desired)[0].status == "unchanged"
    assert len(server.schemas) == len(server.forms) == 1
    assert (
        server.commands[0]["environment-variables"]["PYDRA2APP_ASSESSOR_FORM"]
        == "form-0"
    )


@pytest.mark.parametrize("change", ["schema", "help"])
def test_new_form_revision_preserves_existing_form_and_updates_same_image(change):
    document = make_app().command("test-command").make_json()
    server = AssessorXnat()

    def desired(doc):
        return [
            _DesiredPipeline(
                "pipeline", (_DesiredCommand(doc["name"], doc["image"], doc),)
            )
        ]

    reconcile_commands(server, desired(document))
    old_form = deepcopy(server.forms[0])
    revised = deepcopy(document)
    fields = revised["command-metadata"][METADATA_KEY]["fields"]
    if change == "schema":
        fields.append({"key": "extraScalar", "label": "extra", "type": "textfield"})
    else:
        fields[0]["description"] = "Updated guidance for this result."
    assert reconcile_commands(server, desired(revised))[0].status == "updated"
    assert len(server.schemas) == 1
    assert len(server.forms) == 2
    assert server.forms[0] == old_form
    assert (
        server.commands[0]["environment-variables"]["PYDRA2APP_ASSESSOR_FORM"]
        == "form-1"
    )


def test_form_label_change_creates_revision_without_modifying_existing_form(
    monkeypatch,
):
    server = AssessorXnat()
    definition = assessor_definition(make_app().command("test-command"))
    old_id = ensure_assessor(server, definition)
    old_form = deepcopy(server.forms[0])
    monkeypatch.setitem(
        PROVENANCE_FIELDS, "workflow", ("Container Service workflow ID", "textfield")
    )
    new_id = ensure_assessor(server, definition)
    assert new_id != old_id
    assert len(server.schemas) == 1
    assert server.forms[0] == old_form
    assert ensure_assessor(server, definition) == new_id


@pytest.fixture
def staged_assessor(tmp_path):
    output = tmp_path / "result.txt"
    output.write_text("pipeline result")
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "result.json").write_text(
        json_module.dumps({"path": "result@", "files": [str(output)]})
    )
    return tmp_path


@pytest.fixture
def assessor_result_server():
    assessors = []
    server = Mock()
    server.get.side_effect = lambda *a, **kw: response(
        {"ResultSet": {"Result": assessors}}
    )

    def put(path, **kwargs):
        if not path.startswith("/xapi/"):
            assessors.append(
                {
                    "ID": f"ASSESSOR_{len(assessors)}",
                    "label": unquote(path.rsplit("/", 1)[1]),
                    "xsiType": kwargs["query"]["xsiType"],
                }
            )

    server.put.side_effect = put
    return server, assessors


@pytest.mark.parametrize("legacy_label", [False, True])
def test_finalisation_uses_readable_names_and_reuses_existing_runs(
    staged_assessor, assessor_result_server, legacy_label
):
    definition = assessor_definition(make_app().command("test-command"))
    server, assessors = assessor_result_server
    arguments = {
        "xlogin": server,
        "definition": definition,
        "project": "PROJECT",
        "parent_id": "SESSION_ID",
        "parent_label": "SESSION",
        "staging": staged_assessor,
        "outputs": {"out_file": "result"},
        "form_id": "form-0",
        "provenance": {
            "pipeline": definition["pipeline"],
            "version": "1.0",
            "image": "test/image:1.0",
            "parameters": {"scale": 2.0},
            "workflow": "workflow-1",
        },
    }
    if legacy_label:
        identity = {
            key: arguments["provenance"][key]
            for key in ("pipeline", "version", "image", "parameters")
        }
        identity.update(inputs=[], outputs=arguments["outputs"])
        assessors.append(
            {
                "ID": "ASSESSOR_0",
                "label": f"SESSION_{definition['name']}_1_0_{_digest(identity)[:16]}",
                "xsiType": xsi_type(definition),
            }
        )
    finalise_assessor(**arguments)
    arguments["provenance"]["workflow"] = "workflow-2"
    finalise_assessor(**arguments)
    assert len(assessors) == 1
    if legacy_label:
        assert definition["name"] in assessors[0]["label"]
    else:
        assert "assessor-test_test-command_1_0" in assessors[0]["label"]
        assert definition["name"] not in assessors[0]["label"]
    assert server.upload_file.call_args.args[0] == (
        "/data/experiments/SESSION_ID/assessors/ASSESSOR_0"
        "/out/resources/out_file/files/result.txt"
    )
    assert server.upload_string.call_args.args[0].endswith(
        "/PROVENANCE/files/provenance.json"
    )
    fields = server.put.call_args.kwargs["json"]["form-0"]
    assert json_module.loads(fields["parameters"]) == {"scale": 2.0}
    assert fields["workflow"] == "workflow-2"


def test_finalisation_removes_stale_output_files(
    staged_assessor, assessor_result_server
):
    definition = assessor_definition(make_app().command("test-command"))
    server, assessors = assessor_result_server
    resource = output_resource_name("out_file")
    current_file = (
        "/data/experiments/SESSION_ID/assessors/ASSESSOR_0"
        f"/out/resources/{resource}/files/result.txt"
    )
    stale_file = current_file.rsplit("/", 1)[0] + "/previous-result.txt"

    def get(path, **kwargs):
        if path.endswith("/out/resources"):
            return response({"ResultSet": {"Result": [{"label": resource}]}})
        if path.endswith(f"/out/resources/{resource}/files"):
            return response(
                {"ResultSet": {"Result": [{"URI": current_file}, {"URI": stale_file}]}}
            )
        return response({"ResultSet": {"Result": assessors}})

    server.get.side_effect = get
    server.delete = Mock()
    finalise_assessor(
        xlogin=server,
        definition=definition,
        project="PROJECT",
        parent_id="SESSION_ID",
        parent_label="SESSION",
        staging=staged_assessor,
        outputs={"out_file": "result"},
        form_id="form-0",
        provenance={
            "pipeline": definition["pipeline"],
            "version": "1.0",
            "image": "test/image:1.0",
            "parameters": {"scale": 2.0},
            "workflow": "workflow-1",
        },
    )

    server.delete.assert_called_once_with(stale_file, accepted_status=[200, 204])


def test_readable_run_labels_do_not_collide_across_frequencies(
    staged_assessor, assessor_result_server
):
    server, assessors = assessor_result_server
    for frequency in ("session", "subject", "dataset"):
        definition = assessor_definition(
            make_app(operates_on="medimage/" + frequency).command("test-command")
        )
        finalise_assessor(
            server,
            definition,
            "PROJECT",
            "PARENT_ID",
            "PARENT",
            staged_assessor,
            {"out_file": "result"},
            "form-0",
            {
                "pipeline": definition["pipeline"],
                "version": "1.0",
                "image": "test/image:1.0",
                "parameters": {},
                "workflow": "workflow-1",
            },
        )
    assert len({assessor["label"] for assessor in assessors}) == 3


@pytest.mark.parametrize("name", ["QC/report", "PROVENANCE", "x" * 120])
def test_output_resource_names_are_safe_and_do_not_use_reserved_labels(name):
    resource = output_resource_name(name)
    assert "/" not in resource
    assert len(resource) <= 100
    assert resource.upper() != "PROVENANCE"
    assert resource.endswith("_" + _digest(name)[:16])


def test_colliding_output_labels_fail_before_creating_an_assessor(staged_assessor):
    server = Mock()
    unsafe_name = "out/file"
    with pytest.raises(AssessorError, match="colliding resource labels"):
        finalise_assessor(
            server,
            assessor_definition(make_app().command("test-command")),
            "PROJECT",
            "SESSION_ID",
            "SESSION",
            staged_assessor,
            {unsafe_name: "result", output_resource_name(unsafe_name): "result"},
            "form-0",
            {},
        )
    assert not server.mock_calls


@pytest.mark.parametrize("version", ["1.9.3", "1.8.10"])
def test_older_xnat_keeps_legacy_output_storage(version, caplog):
    server = AssessorXnat(version)
    document = make_app().command("test-command").make_json()
    prepared = prepare_assessor_command(server, document)
    assert prepared["environment-variables"]["PYDRA2APP_ASSESSOR_FORM"] == ""
    assert not server.forms and not server.schemas
    assert "legacy output storage" in caplog.text


def test_registration_permission_failure_does_not_install_command():
    document = make_app().command("test-command").make_json()
    server = AssessorXnat()
    denied = xnat.exceptions.XNATResponseError(
        "Site administrator required",
        SimpleNamespace(
            url="/xapi/datatypes/create", status_code=403, text="Forbidden"
        ),
    )
    desired = [
        _DesiredPipeline(
            "pipeline",
            (_DesiredCommand(document["name"], document["image"], document),),
        )
    ]
    with patch.object(server, "post", side_effect=denied):
        result = reconcile_commands(server, desired)[0]
    assert result.status == "failed"
    assert "Site administrator required" in result.error
    assert not server.commands


@pytest.mark.parametrize(
    "conflict",
    ["prefix", "inheritance", "disabled", "modified", "disabled-type", "root"],
)
def test_registration_conflicts_are_reported_without_replacing_definitions(conflict):
    server = AssessorXnat()
    definition = assessor_definition(make_app().command("test-command"))
    ensure_assessor(server, definition)
    if conflict == "prefix":
        server.schemas[0]["name"] = definition["prefix"] + ":anotherType"
    elif conflict == "inheritance":
        server.schemas[0]["content"] = server.schemas[0]["content"].replace(
            "xnat:imageAssessorData", "xnat:subjectAssessorData"
        )
    elif conflict == "disabled":
        server.forms[0]["appliesToList"][0]["status"] = "disabled"
    elif conflict == "disabled-type":
        server.schemas[0]["enabled"] = False
    elif conflict == "root":
        server.schemas[0]["content"] = server.schemas[0]["content"].replace(
            definition["name"], "UnrelatedPipeline"
        )
    else:
        builder = json_module.loads(server.forms[0]["contents"])
        builder["components"].pop()
        server.forms[0]["contents"] = json_module.dumps(builder)
    calls = len(server.calls)
    with pytest.raises(AssessorError):
        ensure_assessor(server, definition)
    assert len(server.calls) == calls


def test_runtime_finalises_only_after_success(tmp_path, monkeypatch):
    command = make_app().command("test-command")
    store = AssessorOutputStore(
        server="https://unused.example", user=None, password=None, cache_dir=tmp_path
    )
    session = SimpleNamespace(
        projects={
            "PROJECT": SimpleNamespace(
                experiments={
                    "SESSION": SimpleNamespace(id="SESSION_ID", label="SESSION")
                }
            )
        },
    )
    monkeypatch.setattr(AssessorOutputStore, "connect", lambda self: session)
    monkeypatch.setattr(AssessorOutputStore, "disconnect", lambda self, session: None)
    monkeypatch.setenv("PYDRA2APP_ASSESSOR_FORM", "form-0")
    monkeypatch.setenv("XNAT_WORKFLOW_ID", "test-workflow")
    with (
        patch("pydra2app.xnat.assessor_runtime.Store.load", return_value=store),
        patch("pydra2app.xnat.assessor_runtime.supports_assessors", return_value=True),
        patch.object(type(command), "execute", side_effect=RuntimeError("failed")),
        patch("pydra2app.xnat.assessor_runtime.finalise_assessor") as finalise,
    ):
        with pytest.raises(RuntimeError, match="failed"):
            execute_with_assessor(
                command, "xnat-cs//PROJECT", ids=["SESSION"], work_dir=tmp_path
            )
        finalise.assert_not_called()


@pytest.mark.parametrize("workflow", [None, "", " "])
def test_missing_workflow_fails_before_pipeline_execution(
    tmp_path, monkeypatch, workflow
):
    command = make_app().command("test-command")
    store = AssessorOutputStore(
        server="https://unused.example", user=None, password=None, cache_dir=tmp_path
    )
    monkeypatch.setattr(AssessorOutputStore, "connect", lambda self: object())
    monkeypatch.setattr(AssessorOutputStore, "disconnect", lambda self, session: None)
    monkeypatch.setenv("PYDRA2APP_ASSESSOR_FORM", "form-0")
    if workflow is None:
        monkeypatch.delenv("XNAT_WORKFLOW_ID", raising=False)
    else:
        monkeypatch.setenv("XNAT_WORKFLOW_ID", workflow)
    with (
        patch("pydra2app.xnat.assessor_runtime.Store.load", return_value=store),
        patch("pydra2app.xnat.assessor_runtime.supports_assessors", return_value=True),
        patch.object(type(command), "execute") as execute,
    ):
        with pytest.raises(AssessorError, match="XNAT_WORKFLOW_ID"):
            execute_with_assessor(
                command, "xnat-cs//PROJECT", ids=["SESSION"], work_dir=tmp_path
            )
        execute.assert_not_called()


def test_runtime_on_older_xnat_executes_without_staging(tmp_path, monkeypatch):
    command = make_app().command("test-command")
    store = AssessorOutputStore(
        server="https://unused.example", user=None, password=None, cache_dir=tmp_path
    )
    monkeypatch.setattr(AssessorOutputStore, "connect", lambda self: object())
    monkeypatch.setattr(AssessorOutputStore, "disconnect", lambda self, session: None)
    with (
        patch("pydra2app.xnat.assessor_runtime.Store.load", return_value=store),
        patch("pydra2app.xnat.assessor_runtime.supports_assessors", return_value=False),
        patch.object(type(command), "execute") as execute,
        patch("pydra2app.xnat.assessor_runtime.finalise_assessor") as finalise,
    ):
        execute_with_assessor(
            command, "xnat-cs//PROJECT", ids=["SESSION"], work_dir=tmp_path
        )
    assert "assessor_staging_dir" not in execute.call_args.kwargs
    finalise.assert_not_called()


@pytest.mark.skipif(
    os.environ.get("PYDRA2APP_TEST_XNAT_110") != "1",
    reason="Requires an existing local XNAT 1.10 instance; does not launch/reset XNAT",
)
@pytest.mark.parametrize("frequency", ["session", "subject", "dataset"])
def test_live_assessor_pipeline(tmp_path, monkeypatch, frametree_home, frequency):
    """Run real FrameTree execution and verify file, scalar and provenance storage."""
    app = make_app(image_name="assessor-ui", operates_on="medimage/" + frequency)
    command = app.command("test-command")
    definition = assessor_definition(command)
    project = "P2AAssessorTests"
    session_label = "P2A_SESS"
    with xnat4tests.connect() as session:
        assert urlparse(session.server).hostname in {"localhost", "127.0.0.1"}
        session.put(f"/data/projects/{project}")
        session.put(f"/data/projects/{project}/subjects/P2A_SUBJ")
        session.put(
            f"/data/projects/{project}/subjects/P2A_SUBJ/experiments/{session_label}",
            query={"xsiType": "xnat:mrSessionData"},
        )
        xsession = session.projects[project].experiments[session_label]
        session_id = xsession.id
        session_uri = f"/data/experiments/{session_id}"
        scan = session.classes.MrScanData(parent=xsession, id="1", type="input")
        session.upload_string(
            scan.uri + "/resources/TEXT/files/input.txt",
            data=b"assessor output test",
            overwrite=True,
        )
        if frequency == "session":
            collection = session_uri + "/assessors"
            input_path = "input/TEXT"
            resource_segment = "/out/resources/"
            ids = [session_label]
        else:
            subject = session.projects[project].subjects["P2A_SUBJ"]
            parent = subject if frequency == "subject" else session.projects[project]
            collection = (
                f"/data/projects/{project}/subjects/{subject.id}/experiments"
                if frequency == "subject"
                else f"/data/projects/{project}/experiments"
            )
            try:
                input_resource = parent.resources["input"]
            except KeyError:
                input_resource = session.classes.ResourceCatalog(
                    parent=parent, label="input", format=TextFile.mime_like
                )
            session.upload_string(
                input_resource.uri + "/files/input.txt",
                data=b"assessor output test",
                overwrite=True,
            )
            input_path = "input@"
            resource_segment = "/resources/"
            ids = ["P2A_SUBJ"] if frequency == "subject" else None
        prepared = prepare_assessor_command(session, command.make_json())
        assert prepare_assessor_command(session, command.make_json()) == prepared
        datatype = session.get(
            "/xapi/datatypes/" + quote(xsi_type(definition), safe="")
        ).json()
        assert datatype["singular"] == definition["singular"]
        assert datatype["plural"] == definition["plural"]
        form_id = prepared["environment-variables"]["PYDRA2APP_ASSESSOR_FORM"]
        monkeypatch.setenv("PYDRA2APP_ASSESSOR_FORM", form_id)
        monkeypatch.setenv("PYDRA2APP_IMAGE", "test/image@sha256:" + "a" * 64)
        monkeypatch.setenv("XNAT_WORKFLOW_ID", "issue72-test-workflow")
        server = session.server
    monkeypatch.setattr(
        AssessorOutputStore, "connect", lambda self: xnat4tests.connect()
    )
    store = AssessorOutputStore(
        server=server,
        user=None,
        password=None,
        cache_dir=tmp_path / "cache",
        input_mount=tmp_path / "missing-mount",
    )
    store.save("issue72-assessor-test")
    assessor_ids = []

    def finalise(*args, **kwargs):
        assessor_id = finalise_assessor(*args, **kwargs)
        assessor_ids.append(assessor_id)
        return assessor_id

    with patch(
        "pydra2app.xnat.assessor_runtime.finalise_assessor", side_effect=finalise
    ):
        execute_with_assessor(
            command,
            f"issue72-assessor-test//{project}",
            ids=ids,
            dataset_hierarchy="subject,session",
            work_dir=tmp_path / "work",
            input_values={"in_file": input_path},
            output_values={"out_file": "result", "score": "score", "passed": "passed"},
            parameter_values={"scale": "2.0", "fail": "false"},
            raise_errors=True,
            worker="debug",
        )
    assert len(assessor_ids) == 1
    with xnat4tests.connect() as session:
        assessors = session.get(
            collection, format="json", query={"xsiType": xsi_type(definition)}
        ).json()["ResultSet"]["Result"]
        assessor = next(item for item in assessors if item["ID"] == assessor_ids[0])
        assert "assessor-ui_test-command" in assessor["label"]
        uri = (
            session_uri + "/assessors/" + assessor["ID"]
            if frequency == "session"
            else "/data/experiments/" + assessor["ID"]
        )
        fields = session.get(
            f"/xapi/custom-fields/experiments/{assessor['ID']}/fields"
        ).json()[form_id]
        scalar_keys = {field["label"]: field["key"] for field in definition["fields"]}
        assert fields[scalar_keys["score"]] == 40.0
        assert fields[scalar_keys["passed"]] is True
        assert fields["workflow"] == "issue72-test-workflow"
        assert fields["image"].endswith("@sha256:" + "a" * 64)
        provenance = session.get(
            uri + resource_segment + "PROVENANCE/files/provenance.json"
        ).json()
        assert provenance["inputs"][0]["uri"].endswith(
            "/resources/TEXT" if frequency == "session" else "/resources/input"
        )
        resource = "out_file"
        files = session.get(
            uri + resource_segment + resource + "/files", format="json"
        ).json()["ResultSet"]["Result"]
        assert len(files) == 1
        assert session.get(files[0]["URI"]).text == "assessor output test"
        target = xsi_type(definition)
        search_class = next(
            cls
            for cls in vars(session.classes).values()
            if getattr(cls, "_XSI_TYPE", None) == target
        )
        score_field = next(
            name.split("/", 1)[1]
            for name in session.inspect.datafields(target)
            if form_id.upper() in name.upper()
            and name.upper().endswith("_" + scalar_keys["score"].upper())
        )
        score = SearchField(search_class, score_field, type="double")
        for threshold, matches in [(39, True), (41, False)]:
            bundle = (
                Query(
                    search_class,
                    session,
                    fields=[SearchField(search_class, "EXPT_ID", type="string"), score],
                )
                .filter(score > threshold)
                .to_xml()
            )
            # Projection IDs are display-field IDs, not schema paths.
            for field in bundle.findall(".//{http://nrg.wustl.edu/security}field_ID"):
                field.text = field.text.split("/", 1)[1]
            results = session.post(
                "/data/search", format="csv", data=ElementTree.tostring(bundle)
            ).text
            rows = list(csv.reader(results.splitlines()))
            assert (assessor["ID"] in results) is matches
            if matches:
                assert "40" in rows[1] or "40.0" in rows[1]
        assert not session.get(session_uri + "/resources", format="json").json()[
            "ResultSet"
        ]["Result"]


@pytest.mark.skipif(
    os.environ.get("PYDRA2APP_TEST_XNAT_110") != "1"
    or not os.environ.get("PYDRA2APP_TEST_CS_IMAGE"),
    reason="Requires local XNAT 1.10, Container Service and a shell-capable test image",
)
@pytest.mark.parametrize("frequency", ["session", "subject", "dataset"])
def test_live_container_service_wrapper(frequency):
    """Verify native CS parent contexts, derived project IDs and row selection."""
    project = "P2AWrapperTests"
    document = (
        make_app(operates_on="medimage/" + frequency)
        .command("test-command")
        .make_json()
    )
    document["name"] += "." + frequency
    document["image"] = os.environ["PYDRA2APP_TEST_CS_IMAGE"]
    environment = document["environment-variables"]
    environment["PYDRA2APP_TEST_PROJECT"] = "[PROJECT_ID]"
    document["command-line"] = (
        'test "$PYDRA2APP_TEST_PROJECT" = P2AWrapperTests '
        '&& test -n "$XNAT_WORKFLOW_ID"'
    )
    if frequency == "subject":
        document["command-line"] += ' && test "$PYDRA2APP_ROW_SUBJECT_LABEL" = P2A_SUBJ'
    with xnat4tests.connect() as session:
        assert urlparse(session.server).hostname in {"localhost", "127.0.0.1"}
        session.put(f"/data/projects/{project}")
        session.put(f"/data/projects/{project}/subjects/P2A_SUBJ")
        session.put(
            f"/data/projects/{project}/subjects/P2A_SUBJ/experiments/P2A_SESS",
            query={"xsiType": "xnat:mrSessionData"},
        )
        document = prepare_assessor_command(session, document)
        installed = [
            item
            for item in session.get("/xapi/commands").json()
            if item["name"] == document["name"]
        ]
        if installed:
            command_id = installed[0]["id"]
            session.post(f"/xapi/commands/{command_id}", json=document)
        else:
            command_id = session.post("/xapi/commands", json=document).json()
        wrapper_name = document["xnat"][0]["name"]
        enabled_path = f"/xapi/commands/{command_id}/wrappers/{wrapper_name}/enabled"
        session.put(enabled_path)
        session.put(
            f"/xapi/projects/{project}/commands/{command_id}/wrappers/{wrapper_name}/enabled"
        )
        parent_uri = f"/archive/projects/{project}"
        if frequency in ("session", "subject"):
            subject = session.projects[project].subjects["P2A_SUBJ"]
            parent_uri += "/subjects/" + subject.id
        if frequency == "session":
            parent_uri += (
                "/experiments/" + session.projects[project].experiments["P2A_SESS"].id
            )
        wrapper = document["xnat"][0]
        root = quote(wrapper["external-inputs"][0]["name"], safe="")
        previous_ids = {
            item["id"]
            for item in session.get("/xapi/containers").json()
            if item["command-id"] == command_id
        }
        launched = session.post(
            f"/xapi/projects/{project}/commands/{command_id}/wrappers/{wrapper_name}/root/{root}/launch",
            json={
                wrapper["external-inputs"][0]["name"]: parent_uri,
                "in_file": "unused",
            },
        ).json()
        assert launched["status"] == "success"
        assert launched["workflow-id"]
        assert launched["workflow-id"] != "To be assigned"
        deadline = time.monotonic() + 90
        status = "Not created"
        while time.monotonic() < deadline:
            containers = [
                item
                for item in session.get("/xapi/containers").json()
                if item["command-id"] == command_id and item["id"] not in previous_ids
            ]
            if containers:
                status = max(containers, key=lambda item: item["id"])["status"]
                if status in {"Complete", "Failed", "Killed"}:
                    break
            time.sleep(1)
        assert status == "Complete"
        assert (
            max(containers, key=lambda item: item["id"])["env"]["XNAT_WORKFLOW_ID"]
            == launched["workflow-id"]
        )
