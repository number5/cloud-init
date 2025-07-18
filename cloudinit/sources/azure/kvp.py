# Copyright (C) 2022 Microsoft Corporation.
#
# This file is part of cloud-init. See LICENSE file for license information.

import logging
from datetime import datetime, timezone
from typing import Optional

from cloudinit import version
from cloudinit.reporting import handlers, instantiated_handler_registry
from cloudinit.sources.azure import errors

LOG = logging.getLogger(__name__)


def get_kvp_handler() -> Optional[handlers.HyperVKvpReportingHandler]:
    """Get instantiated KVP telemetry handler."""
    kvp_handler = instantiated_handler_registry.registered_items.get(
        "telemetry"
    )
    if not isinstance(kvp_handler, handlers.HyperVKvpReportingHandler):
        return None

    return kvp_handler


def report_via_kvp(report: str) -> bool:
    """Report to host via PROVISIONING_REPORT KVP key."""
    kvp_handler = get_kvp_handler()
    if kvp_handler is None:
        LOG.debug("KVP handler not enabled, skipping host report.")
        return False

    kvp_handler.write_key("PROVISIONING_REPORT", report)
    return True


def report_success_to_host(*, vm_id: Optional[str]) -> bool:
    report = errors.encode_report(
        [
            "result=success",
            f"agent=Cloud-Init/{version.version_string()}",
            f"timestamp={datetime.now(timezone.utc).isoformat()}",
            f"vm_id={vm_id}",
        ]
    )

    return report_via_kvp(report)
