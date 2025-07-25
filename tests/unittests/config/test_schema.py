# This file is part of cloud-init. See LICENSE file for license information.

import importlib
import inspect
import itertools
import json
import logging
import os
import re
import sys
import unittest
from collections import namedtuple
from copy import deepcopy
from errno import EACCES
from pathlib import Path
from textwrap import dedent
from types import ModuleType
from typing import List, Optional, Sequence, Set

import pytest
import yaml

from cloudinit import features, performance
from cloudinit.config.schema import (
    SchemaProblem,
    SchemaType,
    SchemaValidationError,
    annotated_cloudconfig_file,
    get_jsonschema_validator,
    get_meta_doc,
    get_schema,
    get_schema_dir,
    handle_schema_args,
    main,
    netplan_validate_network_schema,
    validate_cloudconfig_file,
    validate_cloudconfig_metaschema,
    validate_cloudconfig_schema,
)
from cloudinit.distros import OSFAMILIES
from cloudinit.safeyaml import load_with_marks
from cloudinit.settings import FREQUENCIES
from cloudinit.sources import DataSourceNotFoundException
from cloudinit.templater import JinjaSyntaxParsingException
from cloudinit.util import load_text_file, write_file
from tests.helpers import cloud_init_project_dir
from tests.hypothesis import given
from tests.hypothesis_jsonschema import from_schema
from tests.unittests.helpers import (
    does_not_raise,
    mock,
    skipUnlessHypothesisJsonSchema,
    skipUnlessJsonSchema,
    skipUnlessJsonSchemaVersionGreaterThan,
)

M_PATH = "cloudinit.config.schema."
DEPRECATED_LOG_LEVEL = 35
VERSIONED_USERDATA_SCHEMA_FILE = "versions.schema.cloud-config.json"


def get_schemas() -> dict:
    """Return all legacy module schemas

    Assumes that module schemas have the variable name "schema"
    """
    return get_module_variable("schema")


def get_metas() -> dict:
    """Return all module metas

    Assumes that module schemas have the variable name "schema"
    """
    return get_module_variable("meta")


def get_module_names() -> List[str]:
    """Return list of module names in cloudinit/config"""
    files = list(
        Path(cloud_init_project_dir("cloudinit/config/")).glob("cc_*.py")
    )

    return [mod.stem for mod in files]


def get_modules() -> List[ModuleType]:
    """Return list of modules in cloudinit/config"""
    return [
        importlib.import_module(f"cloudinit.config.{module}")
        for module in get_module_names()
    ]


def get_module_variable(var_name) -> dict:
    """Inspect modules and get variable from module matching var_name"""
    schemas: dict = {}
    get_modules()
    for k, v in sys.modules.items():
        path = Path(k)
        if "cloudinit.config" == path.stem and path.suffix[1:4] == "cc_":
            module_name = path.suffix[1:]
            members = inspect.getmembers(v)
            schemas[module_name] = None
            for name, value in members:
                if name == var_name:
                    schemas[module_name] = value
                    break
    return schemas


class TestVersionedSchemas:
    @pytest.mark.parametrize(
        "schema,error_msg",
        (
            ({}, None),
            ({"version": "v1"}, None),
            ({"version": "v2"}, "is not one of ['v1']"),
            (
                {"version": "v1", "final_message": -1},
                "is not of type 'string'",
            ),
            ({"version": "v1", "final_message": "some msg"}, None),
        ),
    )
    def test_versioned_cloud_config_schema_is_valid_json(
        self, schema, error_msg
    ):
        schema_dir = get_schema_dir()
        version_schemafile = os.path.join(
            schema_dir, VERSIONED_USERDATA_SCHEMA_FILE
        )
        # Point to local schema files avoid JSON resolver trying to pull the
        # reference from our upstream raw file in github.
        version_schema = json.loads(
            re.sub(
                r"https:\/\/raw.githubusercontent.com\/canonical\/"
                r"cloud-init\/main\/cloudinit\/config\/schemas\/",
                f"file://{schema_dir}/",
                load_text_file(version_schemafile),
            )
        )
        if error_msg:
            with pytest.raises(SchemaValidationError) as context_mgr:
                validate_cloudconfig_schema(
                    schema, schema=version_schema, strict=True
                )
            assert error_msg in str(context_mgr.value)
        else:
            validate_cloudconfig_schema(
                schema, schema=version_schema, strict=True
            )


class TestCheckSchema(unittest.TestCase):
    def test_schema_bools_have_dates(self):
        """ensure that new/changed/deprecated keys have an associated
        version key
        """

        def check_deprecation_keys(schema, search_key):
            if search_key in schema:
                assert f"{search_key}_version" in schema
            for sub_item in schema.values():
                if isinstance(sub_item, dict):
                    check_deprecation_keys(sub_item, search_key)
            return True

        # ensure that check_deprecation_keys works as expected
        assert check_deprecation_keys(
            {"changed": True, "changed_version": "22.3"}, "changed"
        )
        assert check_deprecation_keys(
            {"properties": {"deprecated": True, "deprecated_version": "22.3"}},
            "deprecated",
        )
        assert check_deprecation_keys(
            {
                "properties": {
                    "properties": {"new": True, "new_version": "22.3"}
                }
            },
            "new",
        )
        with pytest.raises(AssertionError):
            check_deprecation_keys({"changed": True}, "changed")
        with pytest.raises(AssertionError):
            check_deprecation_keys(
                {"properties": {"deprecated": True}}, "deprecated"
            )
        with pytest.raises(AssertionError):
            check_deprecation_keys(
                {"properties": {"properties": {"new": True}}}, "new"
            )

        # test the in-repo schema
        schema = get_schema()
        assert check_deprecation_keys(schema, "new")
        assert check_deprecation_keys(schema, "changed")
        assert check_deprecation_keys(schema, "deprecated")


class TestGetSchema:
    def test_static_schema_file_is_valid(self, caplog):
        with caplog.at_level(logging.WARNING):
            get_schema()
        # Assert no warnings parsing our packaged schema file
        warnings = [msg for (_, _, msg) in caplog.record_tuples]
        assert [] == warnings

    def test_get_schema_coalesces_known_schema(self):
        """Every cloudconfig module with schema is listed in allOf keyword."""
        schema = get_schema()
        assert sorted(get_module_names()) == sorted(
            [meta["id"] for meta in get_metas().values() if meta is not None]
        )
        assert "http://json-schema.org/draft-04/schema#" == schema["$schema"]
        assert [
            "$defs",
            "$schema",
            "additionalProperties",
            "allOf",
            "properties",
        ] == sorted(list(schema.keys()))
        # New style schema should be defined in static schema file in $defs
        expected_subschema_defs = [
            {"$ref": "#/$defs/base_config"},
            {"$ref": "#/$defs/cc_ansible"},
            {"$ref": "#/$defs/cc_apk_configure"},
            {"$ref": "#/$defs/cc_apt_configure"},
            {"$ref": "#/$defs/cc_apt_pipelining"},
            {"$ref": "#/$defs/cc_ubuntu_autoinstall"},
            {"$ref": "#/$defs/cc_bootcmd"},
            {"$ref": "#/$defs/cc_byobu"},
            {"$ref": "#/$defs/cc_ca_certs"},
            {"$ref": "#/$defs/cc_chef"},
            {"$ref": "#/$defs/cc_disable_ec2_metadata"},
            {"$ref": "#/$defs/cc_disk_setup"},
            {"$ref": "#/$defs/cc_fan"},
            {"$ref": "#/$defs/cc_final_message"},
            {"$ref": "#/$defs/cc_growpart"},
            {"$ref": "#/$defs/cc_grub_dpkg"},
            {"$ref": "#/$defs/cc_install_hotplug"},
            {"$ref": "#/$defs/cc_keyboard"},
            {"$ref": "#/$defs/cc_keys_to_console"},
            {"$ref": "#/$defs/cc_landscape"},
            {"$ref": "#/$defs/cc_locale"},
            {"$ref": "#/$defs/cc_lxd"},
            {"$ref": "#/$defs/cc_mcollective"},
            {"$ref": "#/$defs/cc_mounts"},
            {"$ref": "#/$defs/cc_ntp"},
            {"$ref": "#/$defs/cc_package_update_upgrade_install"},
            {"$ref": "#/$defs/cc_phone_home"},
            {"$ref": "#/$defs/cc_power_state_change"},
            {"$ref": "#/$defs/cc_puppet"},
            {"$ref": "#/$defs/cc_raspberry_pi"},
            {"$ref": "#/$defs/cc_resizefs"},
            {"$ref": "#/$defs/cc_resolv_conf"},
            {"$ref": "#/$defs/cc_rh_subscription"},
            {"$ref": "#/$defs/cc_rsyslog"},
            {"$ref": "#/$defs/cc_runcmd"},
            {"$ref": "#/$defs/cc_salt_minion"},
            {"$ref": "#/$defs/cc_scripts_vendor"},
            {"$ref": "#/$defs/cc_seed_random"},
            {"$ref": "#/$defs/cc_set_hostname"},
            {"$ref": "#/$defs/cc_set_passwords"},
            {"$ref": "#/$defs/cc_snap"},
            {"$ref": "#/$defs/cc_spacewalk"},
            {"$ref": "#/$defs/cc_ssh_authkey_fingerprints"},
            {"$ref": "#/$defs/cc_ssh_import_id"},
            {"$ref": "#/$defs/cc_ssh"},
            {"$ref": "#/$defs/cc_timezone"},
            {"$ref": "#/$defs/cc_ubuntu_drivers"},
            {"$ref": "#/$defs/cc_ubuntu_pro"},
            {"$ref": "#/$defs/cc_update_etc_hosts"},
            {"$ref": "#/$defs/cc_update_hostname"},
            {"$ref": "#/$defs/cc_users_groups"},
            {"$ref": "#/$defs/cc_wireguard"},
            {"$ref": "#/$defs/cc_write_files"},
            {"$ref": "#/$defs/cc_yum_add_repo"},
            {"$ref": "#/$defs/cc_zypper_add_repo"},
            {"$ref": "#/$defs/reporting_config"},
            {"$ref": "#/$defs/output_config"},
        ]
        found_subschema_defs = []
        legacy_schema_keys = []
        for subschema in schema["allOf"]:
            if "$ref" in subschema:
                found_subschema_defs.append(subschema)
            else:  # Legacy subschema sourced from cc_* module 'schema' attr
                legacy_schema_keys.extend(subschema["properties"].keys())

        assert expected_subschema_defs == found_subschema_defs
        # This list should remain empty unless we induct new modules with
        # legacy schema attributes defined within the cc_module.
        assert [] == sorted(legacy_schema_keys)


MODULE_DATA_YAML_TMPL = """\
{mod_id}:
  name: {name}
  title: My Module
  description:
    My amazing module description
  examples:
  - comment: "comment 1"
    file: {examplefile}
"""


class TestModuleDocs:
    def test_validate_data_file_schema(self, mocker, paths):
        mocker.patch(M_PATH + "read_cfg_paths", return_value=paths)
        root_dir = Path(__file__).parent.parent.parent.parent
        for mod_data_f in root_dir.glob("doc/module-docs/*/data.yaml"):
            docs_metadata = yaml.safe_load(mod_data_f.read_text())
            assert docs_metadata.get(mod_data_f.parent.stem), (
                f"Top-level key in {mod_data_f} doesn't match"
                f" {mod_data_f.parent.stem}"
            )
            assert ["description", "examples", "name", "title"] == sorted(
                docs_metadata[mod_data_f.parent.stem].keys()
            )


class SchemaValidationErrorTest:
    """Test validate_cloudconfig_schema"""

    def test_schema_validation_error_expects_schema_errors(self):
        """SchemaValidationError is initialized from schema_errors."""
        errors = [
            SchemaProblem("key.path", 'unexpected key "junk"'),
            SchemaProblem(
                "key2.path", '"-123" is not a valid "hostname" format'
            ),
        ]
        exception = SchemaValidationError(schema_errors=errors)
        assert isinstance(exception, Exception)
        assert exception.schema_errors == errors
        assert (
            'Cloud config schema errors: key.path: unexpected key "junk", '
            'key2.path: "-123" is not a valid "hostname" format'
            == str(exception)
        )
        assert isinstance(exception, ValueError)


class FakeNetplanParserException(Exception):
    def __init__(self, filename, line, column, message):
        self.filename = filename
        self.line = line
        self.column = column
        self.message = message


class TestNetplanValidateNetworkSchema:
    """Tests for netplan_validate_network_schema.

    Heavily mocked because github.com/canonical/netplan project does not
    have a pyproject.toml or setup.py or pypi release that allows us to
    define tox unittest dependencies.
    """

    @pytest.mark.parametrize(
        "config,expected_log",
        (
            ({}, ""),
            ({"version": 1}, ""),
            (
                {"version": 2},
                "Skipping netplan schema validation. No netplan API available",
            ),
            (
                {"network": {"version": 2}},
                "Skipping netplan schema validation. No netplan API available",
            ),
        ),
    )
    def test_network_config_schema_validation_false_when_skipped(
        self, config, expected_log, caplog, mocker
    ):
        """netplan_validate_network_schema returns false when skipped."""
        mocker.patch(f"{M_PATH}LIBNETPLAN_AVAILABLE", False)
        assert False is netplan_validate_network_schema(config)
        assert expected_log in caplog.text

    @pytest.mark.parametrize(
        "error,error_log",
        (
            (None, ""),
            (
                FakeNetplanParserException(
                    "net.yaml",
                    line=1,
                    column=12,
                    message="incorrect YAML value: yes for dhcp value",
                ),
                r"network-config failed schema validation!.*format-l1.c12: "
                "Invalid netplan schema. incorrect YAML value: yes for dhcp "
                "value",
            ),
        ),
    )
    def test_network_config_schema_validation(
        self, error, error_log, caplog, tmpdir, mocker
    ):
        fake_tmpdir = tmpdir.join("mkdtmp")

        class FakeParser:
            def load_yaml_hierarchy(self, parse_dir):
                # Since we mocked mkdtemp to tmpdir, assert we pass tmpdir
                assert parse_dir == fake_tmpdir
                if error:
                    raise error

        # Mock expected imports
        mocker.patch(f"{M_PATH}LIBNETPLAN_AVAILABLE", True)
        mocker.patch(
            f"{M_PATH}NetplanParserException",
            FakeNetplanParserException,
            create=True,
        )
        mocker.patch(f"{M_PATH}Parser", FakeParser, create=True)
        mocker.patch(
            "cloudinit.config.schema.mkdtemp",
            return_value=fake_tmpdir.strpath,
        )
        with caplog.at_level(logging.WARNING):
            assert netplan_validate_network_schema({"version": 2})
        if error_log:
            assert re.match(error_log, caplog.records[0].msg, re.DOTALL)


class TestValidateCloudConfigSchema:
    """Tests for validate_cloudconfig_schema."""

    @pytest.mark.parametrize(
        "schema, call_count",
        ((None, 1), ({"properties": {"p1": {"type": "string"}}}, 0)),
    )
    @skipUnlessJsonSchema()
    @mock.patch(M_PATH + "get_schema")
    def test_validateconfig_schema_use_full_schema_when_no_schema_param(
        self, get_schema, schema, call_count
    ):
        """Use full schema when schema param is absent."""
        get_schema.return_value = {"properties": {"p1": {"type": "string"}}}
        kwargs = {"config": {"p1": "valid"}}
        if schema:
            kwargs["schema"] = schema
        validate_cloudconfig_schema(**kwargs)
        assert call_count == get_schema.call_count

    @skipUnlessJsonSchema()
    def test_validateconfig_schema_non_strict_emits_warnings(self, caplog):
        """When strict is False validate_cloudconfig_schema emits warnings."""
        schema = {"properties": {"p1": {"type": "string"}}}
        validate_cloudconfig_schema({"p1": -1}, schema=schema, strict=False)
        assert (
            caplog.record_tuples and len(caplog.record_tuples) == 1
        ), caplog.record_tuples
        [(module, log_level, log_msg)] = caplog.record_tuples
        assert "cloudinit.config.schema" == module
        assert logging.WARNING == log_level
        assert (
            "cloud-config failed schema validation!\n"
            "p1: -1 is not of type 'string'" == log_msg
        )

    @skipUnlessJsonSchema()
    def test_validateconfig_schema_sensitive(self, caplog):
        """When log_details=False, ensure details are omitted"""
        schema = {
            "properties": {"hashed_password": {"type": "string"}},
            "additionalProperties": False,
        }
        validate_cloudconfig_schema(
            {"hashed-password": "secret"},
            schema=schema,
            strict=False,
            log_details=False,
        )
        [(module, log_level, log_msg)] = caplog.record_tuples
        assert "cloudinit.config.schema" == module
        assert logging.WARNING == log_level
        assert (
            "cloud-config failed schema validation! You may run "
            "'sudo cloud-init schema --system' to check the details."
            == log_msg
        )

    @skipUnlessJsonSchema()
    def test_validateconfig_schema_emits_warning_on_missing_jsonschema(
        self, caplog
    ):
        """Warning from validate_cloudconfig_schema when missing jsonschema."""
        schema = {"properties": {"p1": {"type": "string"}}}
        with mock.patch.dict("sys.modules", jsonschema=ImportError()):
            validate_cloudconfig_schema({"p1": -1}, schema, strict=True)
        assert "Ignoring schema validation. jsonschema is not present" in (
            caplog.text
        )

    @skipUnlessJsonSchema()
    def test_validateconfig_schema_strict_raises_errors(self):
        """When strict is True validate_cloudconfig_schema raises errors."""
        schema = {"properties": {"p1": {"type": "string"}}}
        with pytest.raises(SchemaValidationError) as context_mgr:
            validate_cloudconfig_schema({"p1": -1}, schema=schema, strict=True)
        assert (
            "Cloud config schema errors: p1: -1 is not of type 'string'"
            == (str(context_mgr.value))
        )

    @skipUnlessJsonSchema()
    def test_validateconfig_schema_honors_formats(self):
        """With strict True, validate_cloudconfig_schema errors on format."""
        schema = {"properties": {"p1": {"type": "string", "format": "email"}}}
        with pytest.raises(SchemaValidationError) as context_mgr:
            validate_cloudconfig_schema(
                {"p1": "-1"}, schema=schema, strict=True
            )
        assert "Cloud config schema errors: p1: '-1' is not a 'email'" == (
            str(context_mgr.value)
        )

    @skipUnlessJsonSchema()
    def test_validateconfig_schema_honors_formats_strict_metaschema(self):
        """With strict and strict_metaschema True, ensure errors on format"""
        schema = {"properties": {"p1": {"type": "string", "format": "email"}}}
        with pytest.raises(SchemaValidationError) as context_mgr:
            validate_cloudconfig_schema(
                {"p1": "-1"},
                schema=schema,
                strict=True,
                strict_metaschema=True,
            )
        assert "Cloud config schema errors: p1: '-1' is not a 'email'" == str(
            context_mgr.value
        )

    @skipUnlessJsonSchemaVersionGreaterThan(version=(3, 0, 0))
    def test_validateconfig_strict_metaschema_do_not_raise_exception(
        self, caplog
    ):
        """With strict_metaschema=True, do not raise exceptions.

        This flag is currently unused, but is intended for run-time validation.
        This should warn, but not raise.
        """
        schema = {"properties": {"p1": {"types": "string", "format": "email"}}}
        validate_cloudconfig_schema(
            {"p1": "-1"}, schema=schema, strict_metaschema=True
        )
        assert (
            "Meta-schema validation failed, attempting to validate config"
            in caplog.text
        )

    @skipUnlessJsonSchema()
    @pytest.mark.parametrize("log_deprecations", [True, False])
    @pytest.mark.parametrize(
        "schema,config,expected_msg",
        [
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "properties": {
                        "a-b": {
                            "type": "string",
                            "deprecated": True,
                            "deprecated_version": "22.1",
                            "new": True,
                            "new_version": "22.1",
                            "description": "<desc>",
                        },
                        "a_b": {"type": "string", "description": "noop"},
                    },
                },
                {"a-b": "asdf"},
                "Deprecated cloud-config provided: a-b: <desc> "
                "Deprecated in version 22.1.",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "properties": {
                        "x": {
                            "oneOf": [
                                {"type": "integer", "description": "noop"},
                                {
                                    "type": "string",
                                    "deprecated": True,
                                    "deprecated_version": "22.1",
                                    "description": "<desc>",
                                },
                            ]
                        },
                    },
                },
                {"x": "+5"},
                "Deprecated cloud-config provided: x: <desc> "
                "Deprecated in version 22.1.",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "properties": {
                        "x": {
                            "allOf": [
                                {"type": "string", "description": "noop"},
                                {
                                    "deprecated": True,
                                    "deprecated_version": "22.1",
                                    "deprecated_description": "<dep desc>",
                                    "description": "<desc>",
                                },
                            ]
                        },
                    },
                },
                {"x": "5"},
                "Deprecated cloud-config provided: x: <desc> "
                "Deprecated in version 22.1. <dep desc>",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "properties": {
                        "x": {
                            "anyOf": [
                                {"type": "integer", "description": "noop"},
                                {
                                    "type": "string",
                                    "deprecated": True,
                                    "deprecated_version": "22.1",
                                    "description": "<desc>",
                                },
                            ]
                        },
                    },
                },
                {"x": "5"},
                "Deprecated cloud-config provided: x: <desc> "
                "Deprecated in version 22.1.",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "properties": {
                        "x": {
                            "type": "string",
                            "deprecated": True,
                            "deprecated_version": "22.1",
                            "description": "<desc>",
                        },
                    },
                },
                {"x": "+5"},
                "Deprecated cloud-config provided: x: <desc> "
                "Deprecated in version 22.1.",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "properties": {
                        "x": {
                            "type": "string",
                            "deprecated": False,
                            "description": "<desc>",
                        },
                    },
                },
                {"x": "+5"},
                None,
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "$defs": {
                        "my_ref": {
                            "deprecated": True,
                            "deprecated_version": "32.3",
                            "description": "<desc>",
                        }
                    },
                    "properties": {
                        "x": {
                            "allOf": [
                                {"type": "string", "description": "noop"},
                                {"$ref": "#/$defs/my_ref"},
                            ]
                        },
                    },
                },
                {"x": "+5"},
                "Deprecated cloud-config provided: x: <desc> "
                "Deprecated in version 32.3.",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "$defs": {
                        "my_ref": {
                            "deprecated": True,
                            "deprecated_version": "27.2",
                        }
                    },
                    "properties": {
                        "x": {
                            "allOf": [
                                {
                                    "type": "string",
                                    "description": "noop",
                                },
                                {"$ref": "#/$defs/my_ref"},
                            ]
                        },
                    },
                },
                {"x": "+5"},
                "Deprecated cloud-config provided: x:  Deprecated in "
                "version 27.2.",
            ),
            (
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "patternProperties": {
                        "^.+$": {
                            "minItems": 1,
                            "deprecated": True,
                            "deprecated_version": "27.2",
                            "description": "<desc>",
                        }
                    },
                },
                {"a-b": "asdf"},
                "Deprecated cloud-config provided: a-b: <desc> "
                "Deprecated in version 27.2.",
            ),
            pytest.param(
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "patternProperties": {
                        "^.+$": {
                            "minItems": 1,
                            "deprecated": True,
                            "deprecated_version": "27.2",
                            "changed": True,
                            "changed_version": "22.2",
                            "changed_description": "Drop ballast.",
                        }
                    },
                },
                {"a-b": "asdf"},
                "Deprecated cloud-config provided: a-b:  Deprecated "
                "in version 27.2., a-b:  Changed in version 22.2. "
                "Drop ballast.",
                id="deprecated_pattern_property_without_description",
            ),
        ],
    )
    def test_validateconfig_logs_deprecations(
        self, schema, config, expected_msg, log_deprecations, caplog
    ):
        with mock.patch.object(
            features, "DEPRECATION_INFO_BOUNDARY", "devel"
        ), mock.patch.object(performance, "Timed"):
            validate_cloudconfig_schema(
                config,
                schema=schema,
                strict_metaschema=True,
                log_deprecations=log_deprecations,
            )
        if expected_msg is None:
            return
        log_record = (M_PATH[:-1], DEPRECATED_LOG_LEVEL, expected_msg)
        if log_deprecations:
            assert log_record == caplog.record_tuples[-1]
        else:
            assert log_record not in caplog.record_tuples


class TestCloudConfigExamples:
    metas = get_metas()
    params = [
        (meta["id"], example)
        for meta in metas.values()
        if meta and meta.get("examples")
        for example in meta.get("examples")
    ]

    @pytest.mark.parametrize("schema_id, example", params)
    @skipUnlessJsonSchema()
    def test_validateconfig_schema_of_example(self, schema_id, example):
        """For a given example in a config module we test if it is valid
        according to the unified schema of all config modules
        """
        schema = get_schema()
        config_load = yaml.safe_load(example)
        # cloud-init-schema-v1 is permissive of additionalProperties at the
        # top-level.
        # To validate specific schemas against known documented examples
        # we need to only define the specific module schema and supply
        # strict=True.
        # TODO(Drop to pop/update once full schema is strict)
        schema.pop("allOf")
        schema.update(schema["$defs"][schema_id])
        schema["additionalProperties"] = False
        # Some module examples reference keys defined in multiple schemas
        supplemental_schemas = {
            "cc_landscape": ["cc_apt_configure"],
            "cc_ubuntu_pro": ["cc_power_state_change"],
            "cc_update_hostname": ["cc_set_hostname"],
            "cc_users_groups": ["cc_ssh_import_id"],
            "cc_disk_setup": ["cc_mounts"],
        }
        for supplement_id in supplemental_schemas.get(schema_id, []):
            supplemental_props = dict(
                [
                    (key, value)
                    for key, value in schema["$defs"][supplement_id][
                        "properties"
                    ].items()
                ]
            )
            schema["properties"].update(supplemental_props)
        validate_cloudconfig_schema(config_load, schema=schema, strict=True)


@pytest.mark.usefixtures("fake_filesystem")
class TestValidateCloudConfigFile:
    """Tests for validate_cloudconfig_file."""

    @pytest.mark.parametrize("annotate", (True, False))
    def test_validateconfig_file_error_on_invalid_header(
        self, annotate, tmpdir
    ):
        """On invalid header, validate_cloudconfig_file errors.

        A SchemaValidationError is raised when the file doesn't begin with
        known headers.
        """
        config_file = tmpdir.join("my.yaml")
        config_file.write("#junk")
        error_msg = (
            f'Unrecognized user-data header in {config_file}: "#junk".\n'
            "Expected first line to be one of: #!, ## template: jinja, "
            "#cloud-boothook, #cloud-config,"
        )
        with pytest.raises(SchemaValidationError, match=error_msg):
            validate_cloudconfig_file(config_file.strpath, {}, annotate)

    @pytest.mark.parametrize("annotate", (True, False))
    def test_validateconfig_file_error_on_non_yaml_scanner_error(
        self, annotate, tmpdir
    ):
        """On non-yaml scan issues, validate_cloudconfig_file errors."""
        # Generate a scanner error by providing text on a single line with
        # improper indent.
        config_file = tmpdir.join("my.yaml")
        config_file.write("#cloud-config\nasdf:\nasdf")
        error_msg = (
            f".*errors: format-l3.c1: File {config_file} is not valid YAML.*"
        )
        with pytest.raises(SchemaValidationError, match=error_msg):
            validate_cloudconfig_file(config_file.strpath, {}, annotate)

    @pytest.mark.parametrize("annotate", (True, False))
    def test_validateconfig_file_error_on_non_yaml_parser_error(
        self, annotate, tmpdir
    ):
        """On non-yaml parser issues, validate_cloudconfig_file errors."""
        config_file = tmpdir.join("my.yaml")
        config_file.write("#cloud-config\n{}}")
        error_msg = (
            f"errors: format-l2.c3: File {config_file} is not valid YAML."
        )
        with pytest.raises(SchemaValidationError, match=error_msg):
            validate_cloudconfig_file(config_file.strpath, {}, annotate)

    @skipUnlessJsonSchema()
    @pytest.mark.parametrize("annotate", (True, False))
    def test_validateconfig_file_strictly_validates_schema(
        self, annotate, tmpdir
    ):
        """validate_cloudconfig_file raises errors on invalid schema."""
        config_file = tmpdir.join("my.yaml")
        schema = {"properties": {"p1": {"type": "string", "format": "string"}}}
        config_file.write("#cloud-config\np1: -1")
        error_msg = (
            "Cloud config schema errors: p1: -1 is not of type 'string'"
        )
        with pytest.raises(SchemaValidationError, match=error_msg):
            validate_cloudconfig_file(config_file.strpath, schema, annotate)

    @skipUnlessJsonSchema()
    @pytest.mark.parametrize("annotate", (True, False))
    def test_validateconfig_file_squelches_duplicate_errors(
        self, annotate, tmpdir
    ):
        """validate_cloudconfig_file raises only unique errors."""
        config_file = tmpdir.join("my.yaml")
        schema = {  # Define duplicate schema definitions in different sections
            "allOf": [
                {"properties": {"p1": {"type": "string", "format": "string"}}},
                {"properties": {"p1": {"type": "string", "format": "string"}}},
            ]
        }
        config_file.write("#cloud-config\np1: -1")
        error_msg = (  # Strict match of full error
            "Cloud config schema errors: p1: -1 is not of type 'string'$"
        )
        with pytest.raises(SchemaValidationError, match=error_msg):
            validate_cloudconfig_file(config_file.strpath, schema, annotate)

    @skipUnlessJsonSchema()
    @pytest.mark.parametrize("annotate", (True, False))
    @mock.patch(M_PATH + "read_cfg_paths")
    @mock.patch("cloudinit.url_helper.time.sleep")
    def test_validateconfig_file_no_cloud_cfg(
        self, m_sleep, read_cfg_paths, annotate, paths, capsys, mocker
    ):
        """validate_cloudconfig_file does noop with empty user-data."""
        schema = {"properties": {"p1": {"type": "string", "format": "string"}}}

        paths.get_ipath = paths.get_ipath_cur
        read_cfg_paths.return_value = paths
        cloud_config_file = paths.get_ipath_cur("cloud_config")
        write_file(cloud_config_file, b"")

        validate_cloudconfig_file(
            config_path=cloud_config_file, schema=schema, annotate=annotate
        )
        out, _err = capsys.readouterr()
        assert (
            f"Empty 'cloud-config' found at {cloud_config_file}."
            " Nothing to validate" in out
        )

    @pytest.mark.parametrize("annotate", (True, False))
    def test_validateconfig_file_raises_jinja_syntax_error(
        self, annotate, tmpdir, mocker, capsys
    ):
        # will throw error because of space between last two }'s
        invalid_jinja_template = "## template: jinja\na:b\nc:{{ d } }"
        mocker.patch("os.path.exists", return_value=True)
        mocker.patch(
            "cloudinit.util.load_text_file",
            return_value=invalid_jinja_template,
        )
        mocker.patch(
            "cloudinit.handlers.jinja_template.load_text_file",
            return_value='{"c": "d"}',
        )
        config_file = tmpdir.join("my.yaml")
        config_file.write(invalid_jinja_template)
        with pytest.raises(SystemExit) as context_manager:
            validate_cloudconfig_file(config_file.strpath, {}, annotate)
        assert 1 == context_manager.value.code

        _out, err = capsys.readouterr()
        expected = (
            "Error:\n"
            "Failed to render templated user-data. "
            + JinjaSyntaxParsingException.format_error_message(
                syntax_error="unexpected '}'",
                line_number=3,
                line_content="c:{{ d } }",
            )
            + "\n"
        )
        assert expected == err


class TestAnnotatedCloudconfigFile:
    def test_annotated_cloudconfig_file_no_schema_errors(self):
        """With no schema_errors, print the original content."""
        content = b"ntp:\n  pools: [ntp1.pools.com]\n"
        _, schemamarks = load_with_marks(content)
        assert content == annotated_cloudconfig_file(
            content,
            schemamarks=schemamarks,
            schema_errors=[],
        )

    def test_annotated_cloudconfig_file_schema_annotates_and_adds_footer(self):
        """With schema_errors, error lines are annotated and a footer added."""
        content = dedent(
            """\
            #cloud-config
            # comment
            ntp:
              pools: [-99, 75]
            """
        )
        expected = dedent(
            """\
            #cloud-config
            # comment
            ntp:		# E1
              pools: [-99, 75]		# E2,E3

            # Errors: -------------
            # E1: Some type error
            # E2: -99 is not a string
            # E3: 75 is not a string

            """
        )
        _, schemamarks = load_with_marks(content[13:])
        schema_errors = [
            SchemaProblem("ntp", "Some type error"),
            SchemaProblem("ntp.pools.0", "-99 is not a string"),
            SchemaProblem("ntp.pools.1", "75 is not a string"),
        ]
        assert expected == annotated_cloudconfig_file(
            content,
            schemamarks=schemamarks,
            schema_errors=schema_errors,
        )

    def test_annotated_cloudconfig_file_annotates_separate_line_items(self):
        """Errors are annotated for lists with items on separate lines."""
        content = dedent(
            """\
            #cloud-config
            # comment
            ntp:
              pools:
                - -99
                - 75
            """
        )
        expected = dedent(
            """\
            ntp:
              pools:
                - -99		# E1
                - 75		# E2
            """
        )
        _, schemamarks = load_with_marks(content[13:])
        schema_errors = [
            SchemaProblem("ntp.pools.0", "-99 is not a string"),
            SchemaProblem("ntp.pools.1", "75 is not a string"),
        ]
        assert expected in annotated_cloudconfig_file(
            content,
            schemamarks=schemamarks,
            schema_errors=schema_errors,
        )

    @skipUnlessJsonSchema()
    def test_annotated_invalid_top_level_key(self, tmp_path: Path, capsys):
        expected_err = dedent(
            """\
            #cloud-config
            invalid_key: value		# E1

            # Errors: -------------
            # E1: Additional properties are not allowed ('invalid_key' was unexpected)
            """  # noqa: E501
        )
        config_file = tmp_path / "my.yaml"
        config_file.write_text("#cloud-config\ninvalid_key: value\n")
        with pytest.raises(
            SchemaValidationError,
            match="errors: invalid_key: Additional properties are not allowed",
        ):
            validate_cloudconfig_file(
                str(config_file), get_schema(), annotate=True
            )
        out, _err = capsys.readouterr()
        assert out.strip() == expected_err.strip()


@mock.patch(M_PATH + "read_cfg_paths")  # called by parse_args help docs
class TestMain:
    exclusive_combinations = itertools.combinations(
        ["--system", "--config-file something"], 2
    )

    @pytest.mark.parametrize("params", exclusive_combinations)
    def test_main_exclusive_args(self, _read_cfg_paths, params, capsys):
        """Main exits non-zero and error on required exclusive args."""
        params = list(itertools.chain(*[a.split() for a in params]))
        with mock.patch("sys.argv", ["mycmd"] + params):
            with pytest.raises(SystemExit) as context_manager:
                main()
        assert 1 == context_manager.value.code

        _out, err = capsys.readouterr()
        expected = (
            "Error:\nExpected one of --config-file or --system arguments\n"
        )
        assert expected == err

    @pytest.mark.parametrize(
        "params,expectation",
        [
            pytest.param(["--system"], pytest.raises(SystemExit)),
        ],
    )
    @mock.patch(M_PATH + "os.getuid", return_value=100)
    def test_main_ignores_schema_type(
        self, _os_getuid, read_cfg_paths, params, expectation, paths, capsys
    ):
        """Main ignores --schema-type param when --system present."""
        read_cfg_paths.return_value = paths
        params = list(itertools.chain(*[a.split() for a in params]))
        with mock.patch(
            "sys.argv", ["mycmd", "--schema-type", "network-config"] + params
        ):
            with expectation:
                main()
        out, _err = capsys.readouterr()
        expected = (
            "WARNING: The --schema-type parameter is inapplicable when "
            "--system is present"
        )
        assert expected in out

    def test_main_missing_args(self, _read_cfg_paths, capsys):
        """Main exits non-zero and reports an error on missing parameters."""
        with mock.patch("sys.argv", ["mycmd"]):
            with pytest.raises(SystemExit) as context_manager:
                main()
        assert 1 == context_manager.value.code

        _out, err = capsys.readouterr()
        expected = (
            "Error:\nExpected one of --config-file or --system arguments\n"
        )
        assert expected == err

    def test_main_absent_config_file(self, _read_cfg_paths, capsys):
        """Main exits non-zero when config file is absent."""
        myargs = ["mycmd", "--annotate", "--config-file", "NOT_A_FILE"]
        with mock.patch("sys.argv", myargs):
            with pytest.raises(SystemExit) as context_manager:
                main()
        assert 1 == context_manager.value.code
        _out, err = capsys.readouterr()
        assert "Error: Config file NOT_A_FILE does not exist\n" == err

    @pytest.mark.parametrize(
        "schema_type,content,expected",
        (
            (None, b"#cloud-config\nntp:", "Valid schema"),
            ("cloud-config", b"#cloud-config\nntp:", "Valid schema"),
            (
                "network-config",
                (
                    b"network: {'version': 2, 'ethernets':"
                    b" {'eth0': {'dhcp': true}}}"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n      - type: dhcp\n"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n      - type: manual\n"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n      - type: static\n"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n      - type: static6\n"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n      - type: dhcp6\n"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n      - type: dhcp4\n"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n      - type: ipv6_slaac\n"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n"
                    b"      - type: ipv6_dhcpv6-stateful\n"
                ),
                "Valid schema",
            ),
            (
                "network-config",
                (
                    b"network:\n version: 1\n config:\n  - type: physical\n"
                    b"    name: eth0\n    subnets:\n"
                    b"      - type: ipv6_dhcpv6-stateless\n"
                ),
                "Valid schema",
            ),
        ),
    )
    @mock.patch("cloudinit.net.netplan.available", return_value=False)
    def test_main_validates_config_file(
        self,
        _netplan_available,
        _read_cfg_paths,
        schema_type,
        content,
        expected,
        tmpdir,
        capsys,
        mocker,
    ):
        """When --config-file parameter is provided, main validates schema."""
        mocker.patch(f"{M_PATH}LIBNETPLAN_AVAILABLE", False)
        myyaml = tmpdir.join("my.yaml")
        myargs = ["mycmd", "--config-file", myyaml.strpath]
        if schema_type:
            myargs += ["--schema-type", schema_type]
        myyaml.write(content)  # shortest ntp schema
        with mock.patch("sys.argv", myargs):
            # Always assert we have no netplan module which triggers
            # schema skip of network-config version: 2 until cloud-init
            # grows internal schema-network-config-v2.json.
            with mock.patch.dict("sys.modules", netplan=ImportError()):
                assert 0 == main(), "Expected 0 exit code"
        out, _err = capsys.readouterr()
        assert expected in out

    @pytest.mark.parametrize(
        "update_path_content_by_key, expected_keys",
        (
            pytest.param(
                {},
                {
                    "ud_key": "cloud_config",
                    "vd_key": "vendor_cloud_config",
                    "vd2_key": "vendor2_cloud_config",
                    "net_key": "network_config",
                },
                id="prefer_processed_data_when_present_and_non_empty",
            ),
            pytest.param(
                {
                    "cloud_config": "",
                    "vendor_cloud_config": "",
                    "vendor2_cloud_config": "",
                },
                {
                    "ud_key": "userdata_raw",
                    "vd_key": "vendordata_raw",
                    "vd2_key": "vendordata2_raw",
                    "net_key": "network_config",
                },
                id="prefer_raw_data_when_processed_is_empty",
            ),
            pytest.param(
                {"cloud_config": "", "userdata_raw": ""},
                {
                    "ud_key": "cloud_config",
                    "vd_key": "vendor_cloud_config",
                    "vd2_key": "vendor2_cloud_config",
                    "net_key": "network_config",
                },
                id="prefer_processed_vd_file_path_when_raw_and_processed_empty",
            ),
        ),
    )
    @mock.patch(M_PATH + "read_cfg_paths")
    @mock.patch(M_PATH + "os.getuid", return_value=0)
    def test_main_processed_data_preference_over_raw_data(
        self,
        _read_cfg_paths,
        _getuid,
        read_cfg_paths,
        update_path_content_by_key,
        expected_keys,
        paths,
        capsys,
    ):
        paths.get_ipath = paths.get_ipath_cur
        read_cfg_paths.return_value = paths
        path_content_by_key = {
            "cloud_config": "#cloud-config\n{}",
            "vendor_cloud_config": "#cloud-config\n{}",
            "vendor2_cloud_config": "#cloud-config\n{}",
            "vendordata_raw": "#cloud-config\n{}",
            "vendordata2_raw": "#cloud-config\n{}",
            "network_config": "{version: 1, config: []}",
            "userdata_raw": "#cloud-config\n{}",
        }
        expected_paths = dict(
            (key, paths.get_ipath_cur(expected_keys[key]))
            for key in expected_keys
        )
        path_content_by_key.update(update_path_content_by_key)
        for path_key, path_content in path_content_by_key.items():
            write_file(paths.get_ipath_cur(path_key), path_content)
        data_types = "user-data, vendor-data, vendor2-data, network-config"
        ud_msg = "  Valid schema user-data"
        if (
            not path_content_by_key["cloud_config"]
            and not path_content_by_key["userdata_raw"]
        ):
            ud_msg = (
                f"Empty 'cloud-config' found at {expected_paths['ud_key']}."
                " Nothing to validate."
            )

        expected = dedent(
            f"""\
        Found cloud-config data types: {data_types}

        1. user-data at {expected_paths["ud_key"]}:
        {ud_msg}

        2. vendor-data at {expected_paths['vd_key']}:
          Valid schema vendor-data

        3. vendor2-data at {expected_paths['vd2_key']}:
          Valid schema vendor2-data

        4. network-config at {expected_paths['net_key']}:
          Valid schema network-config
        """
        )
        myargs = ["mycmd", "--system"]
        with mock.patch("sys.argv", myargs):
            main()
        out, _err = capsys.readouterr()
        assert expected == out

    @pytest.mark.parametrize(
        "net_config,net_output,error_raised",
        (
            pytest.param(
                "network:\n version: 1\n config:\n  - type: physical\n"
                "    name: eth0\n    subnets:\n      - type: dhcp\n",
                "  Valid schema network-config",
                does_not_raise(),
                id="netv1_schema_validated",
            ),
            pytest.param(
                "network:\n version: 2\n ethernets:\n  eth0:\n"
                "   dhcp4: true\n",
                "  Valid schema network-config",
                does_not_raise(),
                id="netv2_schema_validated_non_netplan",
            ),
            pytest.param(
                "network: {}\n",
                "Skipping network-config schema validation on empty config.",
                does_not_raise(),
                id="empty_net_validation_is_skipped",
            ),
            pytest.param(
                "network:\n version: 1\n config:\n  - type: physical\n"
                "   name: eth0\n    subnets:\n      - type: dhcp\n",
                "  Invalid network-config {network_file}",
                pytest.raises(SystemExit),
                id="netv1_schema_errors_handled",
            ),
            pytest.param(
                "network:\n version: 1\n config:\n  - type: physical\n"
                "    name: eth01234567890123\n    subnets:\n"
                "      - type: dhcp\n",
                "  Invalid network-config {network_file}",
                pytest.raises(SystemExit),
                id="netv1_schema_error_on_nic_name_length",
            ),
        ),
    )
    @mock.patch(M_PATH + "read_cfg_paths")
    @mock.patch(M_PATH + "os.getuid", return_value=0)
    @mock.patch("cloudinit.net.netplan.available", return_value=False)
    def test_main_validates_system_userdata_vendordata_and_network_config(
        self,
        _netplan_available,
        _getuid,
        _read_cfg_paths,
        read_cfg_paths,
        net_config,
        net_output,
        error_raised,
        capsys,
        mocker,
        paths,
    ):
        """When --system is provided, main validates all config userdata."""
        mocker.patch(f"{M_PATH}LIBNETPLAN_AVAILABLE", False)
        paths.get_ipath = paths.get_ipath_cur
        read_cfg_paths.return_value = paths
        cloud_config_file = paths.get_ipath_cur("cloud_config")
        write_file(cloud_config_file, b"#cloud-config\nntp:")
        vd_file = paths.get_ipath_cur("vendor_cloud_config")
        write_file(vd_file, b"#cloud-config\nssh_import_id: [me]")
        vd2_file = paths.get_ipath_cur("vendor2_cloud_config")
        write_file(vd2_file, b"#cloud-config\nssh_pwauth: true")
        network_file = paths.get_ipath_cur("network_config")
        write_file(network_file, net_config)
        myargs = ["mycmd", "--system"]
        with error_raised:
            # Always assert we have no netplan module which triggers
            # schema skip of network-config version: 2 until cloud-init
            # grows internal schema-network-config-v2.json.
            with mock.patch.dict("sys.modules", netplan=ImportError()):
                with mock.patch("sys.argv", myargs):
                    main()
        out, _err = capsys.readouterr()

        net_output = net_output.format(network_file=network_file)
        data_types = "user-data, vendor-data, vendor2-data, network-config"
        expected = dedent(
            f"""\
        Found cloud-config data types: {data_types}

        1. user-data at {cloud_config_file}:
          Valid schema user-data

        2. vendor-data at {vd_file}:
          Valid schema vendor-data

        3. vendor2-data at {vd2_file}:
          Valid schema vendor2-data

        4. network-config at {network_file}:
        {net_output}
        """
        )
        assert expected == out

    @mock.patch(M_PATH + "os.getuid", return_value=1000)
    def test_main_system_userdata_requires_root(
        self, _read_cfg_paths, m_getuid, capsys, paths
    ):
        """Non-root user can't use --system param"""
        myargs = ["mycmd", "--system"]
        with mock.patch("sys.argv", myargs):
            with pytest.raises(SystemExit) as context_manager:
                main()
        assert 1 == context_manager.value.code
        _out, err = capsys.readouterr()
        expected = (
            "Error:\nUnable to read system userdata or vendordata as non-root"
            " user. Try using sudo.\n"
        )
        assert expected == err


def _get_meta_doc_examples(file_glob="cloud-config*.txt"):
    exlusion_patterns = [
        "^cloud-config-archive.*",
        "cloud-config-datasources.txt",
    ]
    exclusion_match = f"({'|'.join(exlusion_patterns)})"
    examples_dir = Path(cloud_init_project_dir("doc/examples"))
    assert examples_dir.is_dir()
    return (
        str(f)
        for f in examples_dir.glob(file_glob)
        if not re.match(exclusion_match, f.name)
    )


class TestSchemaDocExamples:
    schema = get_schema()
    net_schema_v1 = get_schema(schema_type=SchemaType.NETWORK_CONFIG_V1)
    net_schema_v2 = get_schema(schema_type=SchemaType.NETWORK_CONFIG_V2)

    @pytest.mark.parametrize("example_path", _get_meta_doc_examples())
    @skipUnlessJsonSchema()
    def test_cloud_config_schema_doc_examples(self, example_path):
        validate_cloudconfig_file(example_path, self.schema)

        # Assert no use of deprecated keys
        validate_cloudconfig_schema(
            config=yaml.safe_load(open(example_path)),
            schema=self.schema,
            strict=True,
        )

    @pytest.mark.parametrize(
        "example_path",
        _get_meta_doc_examples(file_glob="network-config-v1*yaml"),
    )
    @skipUnlessJsonSchema()
    def test_network_config_schema_v1_doc_examples(self, example_path):
        validate_cloudconfig_schema(
            config=yaml.safe_load(open(example_path)),
            schema=self.net_schema_v1,
            schema_type=SchemaType.NETWORK_CONFIG_V1,
            strict=True,
        )

    @pytest.mark.parametrize(
        "example_path",
        _get_meta_doc_examples(file_glob="network-config-v2*yaml"),
    )
    @skipUnlessJsonSchema()
    def test_network_config_schema_v2_doc_examples(self, example_path):
        validate_cloudconfig_schema(
            config=yaml.safe_load(open(example_path)),
            schema=self.net_schema_v2,
            schema_type=SchemaType.NETWORK_CONFIG_V2,
            strict=True,
        )


VALID_PHYSICAL_CONFIG = {
    "type": "physical",
    "name": "a",
    "mac_address": "aa:bb",
    "mtu": 1,
    "subnets": [
        {
            "type": "dhcp6",
            "control": "manual",
            "netmask": "255.255.255.0",
            "gateway": "10.0.0.1",
            "dns_nameservers": ["8.8.8.8"],
            "dns_search": ["find.me"],
            "routes": [
                {
                    "type": "route",
                    "destination": "10.20.0.0/8",
                    "gateway": "a.b.c.d",
                    "metric": 200,
                }
            ],
        }
    ],
}

VALID_BOND_CONFIG = {
    "type": "bond",
    "name": "a",
    "mac_address": "aa:bb",
    "mtu": 1,
    "subnets": [
        {
            "type": "dhcp6",
            "control": "manual",
            "netmask": "255.255.255.0",
            "gateway": "10.0.0.1",
            "dns_nameservers": ["8.8.8.8"],
            "dns_search": ["find.me"],
            "routes": [
                {
                    "type": "route",
                    "destination": "10.20.0.0/8",
                    "gateway": "a.b.c.d",
                    "metric": 200,
                }
            ],
        }
    ],
}


@skipUnlessJsonSchema()
class TestNetworkSchema:
    net_schema = get_schema(schema_type=SchemaType.NETWORK_CONFIG)

    @pytest.mark.parametrize(
        "src_config, schema_type_version, expectation, log",
        (
            pytest.param(
                {"network": {"config": [], "version": 2}},
                SchemaType.NETWORK_CONFIG_V2,
                pytest.raises(
                    SchemaValidationError,
                    match=re.escape(
                        "Additional properties are not allowed ('config' was "
                        "unexpected)"
                    ),
                ),
                "",
                id="net_v2_invalid_config",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 2,
                        "ethernets": {"eno1": {"dhcp4": True}},
                    }
                },
                SchemaType.NETWORK_CONFIG_V2,
                does_not_raise(),
                "",
                id="net_v2_simple_example",
            ),
            pytest.param(
                {
                    "version": 2,
                    "ethernets": {"eno1": {"dhcp4": True}},
                },
                SchemaType.NETWORK_CONFIG_V2,
                does_not_raise(),
                "",
                id="net_v2_no_top_level",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 2,
                        "ethernets": {
                            "id0": {
                                "match": {
                                    "macaddress": "00:11:22:33:44:55",
                                },
                                "wakeonlan": True,
                                "dhcp4": True,
                                "addresses": [
                                    "192.168.14.2/24",
                                    "2001:1::1/64",
                                ],
                                "gateway4": "192.168.14.1",
                                "gateway6": "2001:1::2",
                                "nameservers": {
                                    "search": ["foo.local", "bar.local"],
                                    "addresses": ["8.8.8.8"],
                                },
                                "routes": [
                                    {
                                        "to": "192.0.2.0/24",
                                        "via": "11.0.0.1",
                                        "metric": 3,
                                    },
                                ],
                            },
                            "lom": {
                                "match": {"driver": "ixgbe"},
                                "set-name": "lom1",
                                "dhcp6": True,
                            },
                            "switchports": {
                                "match": {"name": "enp2*"},
                                "mtu": 1280,
                            },
                        },
                        "bonds": {
                            "bond0": {"interfaces": ["id0", "lom"]},
                        },
                        "bridges": {
                            "br0": {
                                "interfaces": ["wlp1s0", "switchports"],
                                "dhcp4": True,
                            },
                        },
                        "vlans": {
                            "en-intra": {
                                "id": 1,
                                "link": "id0",
                                "dhcp4": "yes",
                            },
                        },
                    }
                },
                SchemaType.NETWORK_CONFIG_V2,
                does_not_raise(),
                "",
                id="net_v2_complex_example",
            ),
            pytest.param(
                {"network": {"version": 1}},
                SchemaType.NETWORK_CONFIG_V1,
                pytest.raises(
                    SchemaValidationError,
                    match=re.escape("'config' is a required property"),
                ),
                "",
                id="config_key_required",
            ),
            pytest.param(
                {"network": {"version": 1, "config": []}},
                SchemaType.NETWORK_CONFIG_V1,
                does_not_raise(),
                "",
                id="config_key_required",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 1,
                        "config": [{"name": "me", "type": "typo"}],
                    }
                },
                SchemaType.NETWORK_CONFIG_V1,
                pytest.raises(
                    SchemaValidationError,
                    match=(
                        r"network.config.0: {'name': 'me', 'type': 'typo'} is"
                        " not valid under any of the given schemas"
                    ),
                ),
                "",
                id="unknown_config_type_item",
            ),
            pytest.param(
                {"network": {"version": 1, "config": [{"type": "physical"}]}},
                SchemaType.NETWORK_CONFIG_V1,
                pytest.raises(
                    SchemaValidationError,
                    match=r"network.config.0: 'name' is a required property.*",
                ),
                "",
                id="physical_requires_name_property",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 1,
                        "config": [{"type": "physical", "name": "a"}],
                    }
                },
                SchemaType.NETWORK_CONFIG_V1,
                does_not_raise(),
                "",
                id="physical_with_name_succeeds",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 1,
                        "config": [
                            {"type": "physical", "name": "a", "asdf": 1}
                        ],
                    }
                },
                SchemaType.NETWORK_CONFIG_V1,
                pytest.raises(
                    SchemaValidationError,
                    match=r"Additional properties are not allowed.*",
                ),
                "",
                id="physical_no_additional_properties",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 1,
                        "config": [VALID_PHYSICAL_CONFIG],
                    }
                },
                SchemaType.NETWORK_CONFIG_V1,
                does_not_raise(),
                "",
                id="physical_with_all_known_properties",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 1,
                        "config": [VALID_BOND_CONFIG],
                    }
                },
                SchemaType.NETWORK_CONFIG_V1,
                does_not_raise(),
                "",
                id="bond_with_all_known_properties",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 1,
                        "config": [
                            {"type": "physical", "name": "eth0", "mtu": None},
                            {"type": "nameserver", "address": "8.8.8.8"},
                        ],
                    }
                },
                SchemaType.NETWORK_CONFIG_V1,
                does_not_raise(),
                "",
                id="GH-4710_mtu_none_and_str_address",
            ),
            pytest.param(
                {
                    "network": {
                        "version": 1,
                        "config": [
                            {
                                "type": "physical",
                                "name": "eth0",
                                "subnets": [
                                    {"type": "dhcp4", "metric": 100},
                                    {"type": "dhcp6", "metric": 1000},
                                ],
                            }
                        ],
                    }
                },
                SchemaType.NETWORK_CONFIG_V1,
                does_not_raise(),
                "",
                id="subnet_metric_validation",
            ),
        ),
    )
    @mock.patch("cloudinit.net.netplan.available", return_value=False)
    def test_network_schema(
        self,
        _netplan_available,
        src_config,
        schema_type_version,
        expectation,
        log,
        caplog,
        mocker,
    ):
        mocker.patch(f"{M_PATH}LIBNETPLAN_AVAILABLE", False)
        net_schema = get_schema(schema_type=schema_type_version)
        with expectation:
            validate_cloudconfig_schema(
                config=src_config,
                schema=net_schema,
                schema_type=schema_type_version,
                strict=True,
            )
        if log:
            assert log in caplog.text


class TestStrictMetaschema:
    """Validate that schemas follow a stricter metaschema definition than
    the default. This disallows arbitrary key/value pairs.
    """

    @skipUnlessJsonSchema()
    def test_modules(self):
        """Validate all modules with a stricter metaschema"""
        (validator, _) = get_jsonschema_validator()
        for name, value in get_schemas().items():
            if value:
                validate_cloudconfig_metaschema(validator, value)
            else:
                logging.warning("module %s has no schema definition", name)

    @skipUnlessJsonSchemaVersionGreaterThan(version=(3, 0, 0))
    def test_validate_bad_module(self):
        """Throw exception by default, don't throw if throw=False

        item should be 'items' and is therefore interpreted as an additional
        property which is invalid with a strict metaschema
        """
        (validator, _) = get_jsonschema_validator()
        schema = {
            "type": "array",
            "item": {
                "type": "object",
            },
        }
        with pytest.raises(
            SchemaValidationError,
            match=r"Additional properties are not allowed.*",
        ):
            validate_cloudconfig_metaschema(validator, schema)

        validate_cloudconfig_metaschema(validator, schema, throw=False)


class TestMeta:
    def test_valid_meta_for_every_module(self):
        all_distros = {
            name for distro in OSFAMILIES.values() for name in distro
        }
        all_distros.add("all")
        for module in get_modules():
            assert "frequency" in module.meta
            assert "distros" in module.meta
            assert {module.meta["frequency"]}.issubset(FREQUENCIES)
            assert set(module.meta["distros"]).issubset(all_distros)


def remove_modules(schema, modules: Set[str]) -> dict:
    indices_to_delete = set()
    for module in set(modules):
        for index, ref_dict in enumerate(schema["allOf"]):
            if ref_dict["$ref"] == f"#/$defs/{module}":
                indices_to_delete.add(index)
                continue  # module found
    for index in indices_to_delete:
        schema["allOf"].pop(index)
    return schema


def remove_defs(schema, defs: Set[str]) -> dict:
    defs_to_delete = set(schema["$defs"].keys()).intersection(set(defs))
    for key in defs_to_delete:
        del schema["$defs"][key]
    return schema


def clean_schema(
    schema=None,
    modules: Optional[Sequence[str]] = None,
    defs: Optional[Sequence[str]] = None,
):
    schema = deepcopy(schema or get_schema())
    if modules:
        remove_modules(schema, set(modules))
    if defs:
        remove_defs(schema, set(defs))
    del schema["properties"]
    del schema["additionalProperties"]
    return schema


@pytest.mark.hypothesis_slow
class TestSchemaFuzz:
    # Avoid https://github.com/Zac-HD/hypothesis-jsonschema/issues/97
    SCHEMA = clean_schema(
        modules=["cc_users_groups"],
        defs=["users_groups.groups_by_groupname", "users_groups.user"],
    )

    @skipUnlessHypothesisJsonSchema()
    @given(from_schema(SCHEMA))
    def test_validate_full_schema(self, orig_config):
        config = deepcopy(orig_config)
        valid_props = get_schema()["properties"].keys()
        for key in orig_config.keys():
            if key not in valid_props:
                del config[key]
        try:
            validate_cloudconfig_schema(config, strict=True)
        except SchemaValidationError as ex:
            if ex.has_errors():
                raise


class TestHandleSchemaArgs:
    Args = namedtuple(
        "Args", "config_file schema_type docs system annotate instance_data"
    )

    @pytest.mark.parametrize(
        "failure, expected_logs",
        (
            (
                IOError("No permissions on /var/lib/cloud/instance"),
                ["Using default instance-data/user-data paths for non-root"],
            ),
            (
                DataSourceNotFoundException("No cached datasource found yet"),
                ["datasource not detected"],
            ),
        ),
    )
    @mock.patch(M_PATH + "read_cfg_paths")
    def test_handle_schema_unable_to_read_cfg_paths(
        self,
        read_cfg_paths,
        failure,
        expected_logs,
        paths,
        capsys,
        caplog,
        tmpdir,
    ):
        if isinstance(failure, IOError):
            failure.errno = EACCES
        read_cfg_paths.side_effect = [failure, paths]
        user_data_fn = tmpdir.join("user-data")
        with open(user_data_fn, "w") as f:
            f.write(
                dedent(
                    """\
                    #cloud-config
                    packages: [sl]
                    """
                )
            )
        args = self.Args(
            config_file=str(user_data_fn),
            schema_type="cloud-config",
            annotate=False,
            docs=None,
            system=None,
            instance_data=None,
        )
        handle_schema_args("unused", args)
        assert "Valid schema" in capsys.readouterr().out
        for expected_log in expected_logs:
            assert expected_log in caplog.text

    @pytest.mark.parametrize(
        "annotate, deprecation_info_boundary, expected_output",
        [
            pytest.param(
                True,
                "devel",
                dedent(
                    """\
                    #cloud-config
                    packages:
                    - htop
                    apt_update: true                # D1
                    apt_upgrade: true               # D2
                    apt_reboot_if_required: true            # D3

                    # Deprecations: -------------
                    # D1: Deprecated in version 22.2. Use **package_update** instead.
                    # D2: Deprecated in version 22.2. Use **package_upgrade** instead.
                    # D3: Deprecated in version 22.2. Use **package_reboot_if_required** instead.

                    Valid schema {cfg_file}
                    """  # noqa: E501
                ),
                id="test_annotated_deprecation_info_boundary_devel_shows",
            ),
            pytest.param(
                True,
                "22.1",
                dedent(
                    """\
                    #cloud-config
                    packages:
                    - htop
                    apt_update: true                # D1
                    apt_upgrade: true               # D2
                    apt_reboot_if_required: true            # D3

                    # Deprecations: -------------
                    # D1: Deprecated in version 22.2. Use **package_update** instead.
                    # D2: Deprecated in version 22.2. Use **package_upgrade** instead.
                    # D3: Deprecated in version 22.2. Use **package_reboot_if_required** instead.

                    Valid schema {cfg_file}
                    """  # noqa: E501
                ),
                id="test_annotated_deprecation_info_boundary_below_unredacted",
            ),
            pytest.param(
                False,
                "18.2",
                dedent(
                    """\
                    Cloud config schema deprecations: \
apt_reboot_if_required: Deprecated in version 22.2. Use\
 **package_reboot_if_required** instead., apt_update: Deprecated in version\
 22.2. Use **package_update** instead., apt_upgrade: Deprecated in version\
 22.2. Use **package_upgrade** instead.\
                    Valid schema {cfg_file}
                    """  # noqa: E501
                ),
                id="test_deprecation_info_boundary_does_unannotated_unredacted",
            ),
        ],
    )
    @mock.patch(M_PATH + "read_cfg_paths")
    def test_handle_schema_args_annotate_deprecated_config(
        self,
        read_cfg_paths,
        annotate,
        deprecation_info_boundary,
        expected_output,
        paths,
        caplog,
        capsys,
        tmpdir,
        mocker,
    ):
        paths.get_ipath = paths.get_ipath_cur
        read_cfg_paths.return_value = paths
        user_data_fn = tmpdir.join("user-data")
        with open(user_data_fn, "w") as f:
            f.write(
                dedent(
                    """\
                    #cloud-config
                    packages:
                    - htop
                    apt_update: true
                    apt_upgrade: true
                    apt_reboot_if_required: true
                    """
                )
            )
        mocker.patch.object(
            features, "DEPRECATION_INFO_BOUNDARY", deprecation_info_boundary
        )
        args = self.Args(
            config_file=str(user_data_fn),
            schema_type="cloud-config",
            annotate=annotate,
            docs=None,
            system=None,
            instance_data=None,
        )
        handle_schema_args("unused", args)
        out, err = capsys.readouterr()
        assert (
            expected_output.format(cfg_file=user_data_fn).split()
            == out.split()
        )
        assert not err
        assert "deprec" not in caplog.text

    @pytest.mark.parametrize(
        "uid, annotate, expected_out, expected_err, expectation",
        [
            pytest.param(
                0,
                True,
                dedent(
                    """\
                    #cloud-config
                    hostname: 123		# E1

                    # Errors: -------------
                    # E1: 123 is not of type 'string'


                    """  # noqa: E501
                ),
                """Error: Invalid schema: user-data\n\n""",
                pytest.raises(SystemExit),
                id="root_annotate_errors_with_exception",
            ),
            pytest.param(
                0,
                False,
                dedent(
                    """\
                    Invalid user-data {cfg_file}
                    """  # noqa: E501
                ),
                dedent(
                    """\
                    Error: Cloud config schema errors: hostname: 123 is not of type 'string'

                    Error: Invalid schema: user-data

                    """  # noqa: E501
                ),
                pytest.raises(SystemExit),
                id="root_no_annotate_exception_with_unique_errors",
            ),
        ],
    )
    @mock.patch(M_PATH + "os.getuid")
    @mock.patch(M_PATH + "read_cfg_paths")
    def test_handle_schema_args_jinja_with_errors(
        self,
        read_cfg_paths,
        getuid,
        uid,
        annotate,
        expected_out,
        expected_err,
        expectation,
        paths,
        caplog,
        capsys,
        tmpdir,
    ):
        getuid.return_value = uid
        paths.get_ipath = paths.get_ipath_cur
        read_cfg_paths.return_value = paths
        user_data_fn = tmpdir.join("user-data")
        if uid == 0:
            id_path = paths.get_runpath("instance_data_sensitive")
        else:
            id_path = paths.get_runpath("instance_data")
        with open(id_path, "w") as f:
            f.write(json.dumps({"ds": {"asdf": 123}}))
        with open(user_data_fn, "w") as f:
            f.write(
                dedent(
                    """\
                    ## template: jinja
                    #cloud-config
                    hostname: {{ ds.asdf }}
                    """
                )
            )
        args = self.Args(
            config_file=str(user_data_fn),
            schema_type="cloud-config",
            annotate=annotate,
            docs=None,
            system=None,
            instance_data=None,
        )
        with expectation:
            handle_schema_args("unused", args)
        out, err = capsys.readouterr()
        assert (
            expected_out.format(cfg_file=user_data_fn, id_path=id_path) == out
        )
        assert (
            expected_err.format(cfg_file=user_data_fn, id_path=id_path) == err
        )
        assert "deprec" not in caplog.text
        assert read_cfg_paths.call_args_list == [
            mock.call(fetch_existing_datasource="trust")
        ]

    @pytest.mark.parametrize(
        "uid, annotate, expected_out, expected_err, expectation",
        [
            pytest.param(
                0,
                False,
                dedent(
                    """\
                    Invalid user-data {cfg_file}
                    """  # noqa: E501
                ),
                dedent(
                    """\
                    Error: Cloud config schema errors: format-l1.c1: Unrecognized user-data header in {cfg_file}: "#bogus-config".
                    Expected first line to be one of: #!, ## template: jinja, #cloud-boothook, #cloud-config, #cloud-config-archive, #cloud-config-jsonp, #include, #include-once, #part-handler

                    Error: Invalid schema: user-data

                    """  # noqa: E501
                ),
                pytest.raises(SystemExit),
                id="root_no_annotate_exception_with_unique_errors",
            ),
        ],
    )
    @mock.patch(M_PATH + "os.getuid")
    @mock.patch(M_PATH + "read_cfg_paths")
    def test_handle_schema_args_unknown_header(
        self,
        read_cfg_paths,
        getuid,
        uid,
        annotate,
        expected_out,
        expected_err,
        expectation,
        paths,
        caplog,
        capsys,
        tmpdir,
    ):
        getuid.return_value = uid
        paths.get_ipath = paths.get_ipath_cur
        read_cfg_paths.return_value = paths
        user_data_fn = tmpdir.join("user-data")
        if uid == 0:
            id_path = paths.get_runpath("instance_data_sensitive")
        else:
            id_path = paths.get_runpath("instance_data")
        with open(user_data_fn, "w") as f:
            f.write(
                dedent(
                    """\
                    #bogus-config
                    hostname: notgonnamakeit
                    """
                )
            )
        args = self.Args(
            config_file=str(user_data_fn),
            schema_type=None,
            annotate=annotate,
            docs=None,
            system=None,
            instance_data=None,
        )
        with expectation:
            handle_schema_args("unused", args)
        out, err = capsys.readouterr()
        assert (
            expected_out.format(cfg_file=user_data_fn, id_path=id_path) == out
        )
        assert (
            expected_err.format(cfg_file=user_data_fn, id_path=id_path) == err
        )
        assert "deprec" not in caplog.text
        assert read_cfg_paths.call_args_list == [
            mock.call(fetch_existing_datasource="trust")
        ]


class TestDeprecation:
    def test_get_meta_doc_deprecation(self, caplog):
        """Test that calling get_meta_doc() emits deprecation.

        Ensures that custom modules calling `get_meta_doc()` can still
        function but receive deprecation warning.
        """
        get_meta_doc("some", "random", "arguments", plus="kwargs")
        assert "The 'get_meta_doc()' function is deprecated" in caplog.text
