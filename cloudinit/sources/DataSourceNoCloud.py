# Copyright (C) 2009-2010 Canonical Ltd.
# Copyright (C) 2012, 2013 Hewlett-Packard Development Company, L.P.
# Copyright (C) 2012 Yahoo! Inc.
#
# Author: Scott Moser <scott.moser@canonical.com>
# Author: Juerg Hafliger <juerg.haefliger@hp.com>
# Author: Joshua Harlow <harlowja@yahoo-inc.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import errno
import logging
import os
from functools import partial

from cloudinit import dmi, lifecycle, sources, util
from cloudinit.net import eni

LOG = logging.getLogger(__name__)


class DataSourceNoCloud(sources.DataSource):

    dsname = "NoCloud"

    def __init__(self, sys_cfg, distro, paths):
        sources.DataSource.__init__(self, sys_cfg, distro, paths)
        self.seed = None
        self.seed_dirs = [
            os.path.join(paths.seed_dir, "nocloud"),
            os.path.join(paths.seed_dir, "nocloud-net"),
        ]
        self.seed_dir = None
        self.supported_seed_starts = ("/", "file://")
        self._network_config = None
        self._network_eni = None

    def __str__(self):
        """append seed and dsmode info when they contain non-default values"""
        return (
            super().__str__()
            + " "
            + (f"[seed={self.seed}]" if self.seed else "")
            + (
                f"[dsmode={self.dsmode}]"
                if self.dsmode != sources.DSMODE_NETWORK
                else ""
            )
        )

    def _get_devices(self, label):
        fslist = util.find_devs_with("TYPE=vfat")
        fslist.extend(util.find_devs_with("TYPE=iso9660"))

        label_list = util.find_devs_with("LABEL=%s" % label.upper())
        label_list.extend(util.find_devs_with("LABEL=%s" % label.lower()))
        label_list.extend(util.find_devs_with("LABEL_FATBOOT=%s" % label))

        devlist = list(set(fslist) & set(label_list))
        devlist.sort(reverse=True)
        return devlist

    def _get_data(self):
        defaults = {
            "instance-id": "nocloud",
            "dsmode": self.dsmode,
        }

        found = []
        mydata = {
            "meta-data": {},
            "user-data": "",
            "vendor-data": "",
            "network-config": None,
        }

        try:
            # Parse the system serial label from dmi. If not empty, try parsing
            # like the command line
            md = {}
            serial = dmi.read_dmi_data("system-serial-number")
            if serial and load_cmdline_data(md, serial):
                found.append("dmi")
                mydata = _merge_new_seed(mydata, {"meta-data": md})
        except Exception:
            util.logexc(LOG, "Unable to parse dmi data")
            return False

        try:
            # Parse the kernel command line, getting data passed in
            md = {}
            if load_cmdline_data(md):
                found.append("cmdline")
                mydata = _merge_new_seed(mydata, {"meta-data": md})
        except Exception:
            util.logexc(LOG, "Unable to parse command line data")
            return False

        # Check to see if the seed dir has data.
        pp2d_kwargs = {
            "required": ["user-data", "meta-data"],
            "optional": ["vendor-data", "network-config"],
        }

        for path in self.seed_dirs:
            try:
                seeded = util.pathprefix2dict(path, **pp2d_kwargs)
                found.append(path)
                LOG.debug("Using seeded data from %s", path)
                mydata = _merge_new_seed(mydata, seeded)
                break
            except ValueError:
                pass

        # If the datasource config had a 'seedfrom' entry, then that takes
        # precedence over a 'seedfrom' that was found in a filesystem
        # but not over external media
        if self.ds_cfg.get("seedfrom"):
            found.append("ds_config_seedfrom")
            mydata["meta-data"]["seedfrom"] = self.ds_cfg["seedfrom"]

        # fields appropriately named can also just come from the datasource
        # config (ie, 'user-data', 'meta-data', 'vendor-data' there)
        if "user-data" in self.ds_cfg and "meta-data" in self.ds_cfg:
            mydata = _merge_new_seed(mydata, self.ds_cfg)
            found.append("ds_config")

        def _pp2d_callback(mp, data):
            return util.pathprefix2dict(mp, **data)

        label = self.ds_cfg.get("fs_label", "cidata")
        if label is not None:
            for dev in self._get_devices(label):
                try:
                    LOG.debug("Attempting to use data from %s", dev)

                    try:
                        seeded = util.mount_cb(
                            dev, _pp2d_callback, pp2d_kwargs
                        )
                    except ValueError:
                        LOG.warning(
                            "device %s with label=%s not a valid seed.",
                            dev,
                            label,
                        )
                        continue

                    mydata = _merge_new_seed(mydata, seeded)

                    LOG.debug("Using data from %s", dev)
                    found.append(dev)
                    break
                except OSError as e:
                    if e.errno != errno.ENOENT:
                        raise
                except util.MountFailedError:
                    util.logexc(
                        LOG, "Failed to mount %s when looking for data", dev
                    )

        # There was no indication on kernel cmdline or data
        # in the seeddir suggesting this handler should be used.
        if not found:
            return False

        # The special argument "seedfrom" indicates we should
        # attempt to seed the userdata / metadata from its value
        # its primarily value is in allowing the user to type less
        # on the command line, ie: ds=nocloud;s=http://bit.ly/abcdefg/
        if "seedfrom" in mydata["meta-data"]:
            seedfrom = mydata["meta-data"]["seedfrom"]
            seedfound = False
            for proto in self.supported_seed_starts:
                if seedfrom.startswith(proto):
                    seedfound = proto
                    break
            if not seedfound:
                self._log_unusable_seedfrom(seedfrom)
                return False
            # check and replace instances of known dmi.<dmi_keys> such as
            # chassis-serial-number or baseboard-product-name
            seedfrom = dmi.sub_dmi_vars(seedfrom)

            # This could throw errors, but the user told us to do it
            # so if errors are raised, let them raise
            md_seed, ud, vd, network = util.read_seeded(seedfrom, timeout=None)
            LOG.debug("Using seeded cache data from %s", seedfrom)

            # Values in the command line override those from the seed
            mydata["meta-data"] = util.mergemanydict(
                [mydata["meta-data"], md_seed]
            )
            mydata["user-data"] = ud
            mydata["vendor-data"] = vd
            mydata["network-config"] = network
            found.append(seedfrom)

        # Now that we have exhausted any other places merge in the defaults
        mydata["meta-data"] = util.mergemanydict(
            [mydata["meta-data"], defaults]
        )

        self.dsmode = self._determine_dsmode(
            [mydata["meta-data"].get("dsmode")]
        )

        if self.dsmode == sources.DSMODE_DISABLED:
            LOG.debug(
                "%s: not claiming datasource, dsmode=%s", self, self.dsmode
            )
            return False

        self.seed = ",".join(found)
        self.metadata = mydata["meta-data"]
        self.userdata_raw = mydata["user-data"]
        self.vendordata_raw = mydata["vendor-data"]
        self._network_config = mydata["network-config"]
        self._network_eni = mydata["meta-data"].get("network-interfaces")
        return True

    @property
    def platform_type(self):
        if not self._platform_type:
            self._platform_type = "lxd" if util.is_lxd() else "nocloud"
        return self._platform_type

    def _log_unusable_seedfrom(self, seedfrom: str):
        """Stage-specific level and message."""
        LOG.info(
            "%s only uses seeds starting with %s - will try to use %s "
            "in the network stage.",
            self,
            self.supported_seed_starts,
            seedfrom,
        )

    def _get_cloud_name(self):
        """Return unknown when 'cloud-name' key is absent from metadata."""
        return sources.METADATA_UNKNOWN

    def _get_subplatform(self):
        """Return the subplatform metadata source details."""
        if self.seed.startswith("/dev"):
            subplatform_type = "config-disk"
        else:
            subplatform_type = "seed-dir"
        return "%s (%s)" % (subplatform_type, self.seed)

    def check_instance_id(self, sys_cfg):
        # quickly (local check only) if self.instance_id is still valid
        # we check kernel command line or files.
        current = self.get_instance_id()
        if not current:
            return None

        # LP: #1568150 need getattr in the case that an old class object
        # has been loaded from a pickled file and now executing new source.
        dirs = getattr(self, "seed_dirs", [self.seed_dir])
        quick_id = _quick_read_instance_id(dirs=dirs)
        if not quick_id:
            return None
        return quick_id == current

    @property
    def network_config(self):
        if self._network_config is None:
            if self._network_eni is not None:
                lifecycle.deprecate(
                    deprecated="Eni network configuration in NoCloud",
                    deprecated_version="24.3",
                    extra_message=(
                        "You can use network v1 or network v2 instead"
                    ),
                )
                self._network_config = eni.convert_eni_data(self._network_eni)
        return self._network_config


def _quick_read_instance_id(dirs=None):
    if dirs is None:
        dirs = []

    iid_key = "instance-id"
    fill = {}
    if load_cmdline_data(fill) and iid_key in fill:
        return fill[iid_key]

    for d in dirs:
        if d is None:
            continue
        try:
            data = util.pathprefix2dict(d, required=["meta-data"])
            md = util.load_yaml(data["meta-data"])
            if md and iid_key in md:
                return md[iid_key]
        except ValueError:
            pass

    return None


def load_cmdline_data(fill, cmdline=None):
    pairs = [
        ("ds=nocloud", sources.DSMODE_LOCAL),
        ("ds=nocloud-net", sources.DSMODE_NETWORK),
    ]
    for idstr, dsmode in pairs:
        if not parse_cmdline_data(idstr, fill, cmdline):
            continue
        if "dsmode" in fill:
            # if dsmode was explicitly in the command line, then
            # prefer it to the dsmode based on seedfrom type
            return True

        seedfrom = fill.get("seedfrom")
        if seedfrom:
            if seedfrom.startswith(
                ("http://", "https://", "ftp://", "ftps://")
            ):
                fill["dsmode"] = sources.DSMODE_NETWORK
            elif seedfrom.startswith(("file://", "/")):
                fill["dsmode"] = sources.DSMODE_LOCAL
        else:
            fill["dsmode"] = dsmode

        return True
    return False


# Returns true or false indicating if cmdline indicated
# that this module should be used.  Updates dictionary 'fill'
# with data that was found.
# Example cmdline:
#  root=LABEL=uec-rootfs ro ds=nocloud
def parse_cmdline_data(ds_id, fill, cmdline=None):
    if cmdline is None:
        cmdline = util.get_cmdline()
    cmdline = " %s " % cmdline

    if not (" %s " % ds_id in cmdline or " %s;" % ds_id in cmdline):
        return False

    argline = ""
    # cmdline can contain:
    # ds=nocloud[;key=val;key=val]
    for tok in cmdline.split():
        if tok.startswith(ds_id):
            argline = tok.split("=", 1)

    # argline array is now 'nocloud' followed optionally by
    # a ';' and then key=value pairs also terminated with ';'
    tmp = argline[1].split(";")
    if len(tmp) > 1:
        kvpairs = tmp[1:]
    else:
        kvpairs = ()

    # short2long mapping to save cmdline typing
    s2l = {"h": "local-hostname", "i": "instance-id", "s": "seedfrom"}
    for item in kvpairs:
        if item == "":
            continue
        try:
            (k, v) = item.split("=", 1)
        except Exception:
            k = item
            v = None
        if k in s2l:
            k = s2l[k]
        fill[k] = v

    return True


def _merge_new_seed(cur, seeded):
    ret = cur.copy()

    newmd = seeded.get("meta-data", {})
    if not isinstance(seeded["meta-data"], dict):
        newmd = util.load_yaml(seeded["meta-data"])
    ret["meta-data"] = util.mergemanydict([cur["meta-data"], newmd])

    if seeded.get("network-config"):
        ret["network-config"] = util.load_yaml(seeded.get("network-config"))

    if "user-data" in seeded:
        ret["user-data"] = seeded["user-data"]
    if "vendor-data" in seeded:
        ret["vendor-data"] = seeded["vendor-data"]
    return ret


class DataSourceNoCloudNet(DataSourceNoCloud):
    def __init__(self, sys_cfg, distro, paths):
        DataSourceNoCloud.__init__(self, sys_cfg, distro, paths)
        self.supported_seed_starts = (
            "http://",
            "https://",
            "ftp://",
            "ftps://",
        )

    def _log_unusable_seedfrom(self, seedfrom: str):
        """Stage-specific level and message."""
        LOG.warning(
            "%s only uses seeds starting with %s - %s is not valid.",
            self,
            self.supported_seed_starts,
            seedfrom,
        )

    def ds_detect(self):
        """Check dmi and kernel command line for dsname

        NoCloud historically used "nocloud-net" as its dsname
        for network timeframe (DEP_NETWORK), which supports http(s) urls.
        For backwards compatiblity, check for that dsname.
        """
        log_deprecated = partial(
            lifecycle.deprecate,
            deprecated="The 'nocloud-net' datasource name",
            deprecated_version="24.1",
            extra_message=(
                "Use 'nocloud' instead, which uses the seedfrom protocol"
                "scheme (http// or file://) to decide how to run."
            ),
        )

        if "nocloud-net" == sources.parse_cmdline():
            log_deprecated()
            return True

        serial = sources.parse_cmdline_or_dmi(
            dmi.read_dmi_data("system-serial-number") or ""
        ).lower()

        if serial in (self.dsname.lower(), "nocloud-net"):
            LOG.debug(
                "Machine is configured by dmi serial number to run on "
                "single datasource %s.",
                self,
            )
            if serial == "nocloud-net":
                log_deprecated()
            return True
        elif (
            self.sys_cfg.get("datasource", {})
            .get("NoCloud", {})
            .get("seedfrom")
        ):
            LOG.debug(
                "Machine is configured by system configuration to run on "
                "single datasource %s.",
                self,
            )
            return True
        return False


# Used to match classes to dependencies
datasources = [
    (DataSourceNoCloud, (sources.DEP_FILESYSTEM,)),
    (DataSourceNoCloudNet, (sources.DEP_FILESYSTEM, sources.DEP_NETWORK)),
]


# Return a list of data sources that match this set of dependencies
def get_datasource_list(depends):
    return sources.list_from_depends(depends, datasources)


if __name__ == "__main__":
    from sys import argv

    logging.basicConfig(level=logging.DEBUG)
    seedfrom = argv[1]
    md_seed, ud, vd, network = util.read_seeded(seedfrom)
    print(f"seeded: {md_seed}")
    print(f"ud: {ud}")
    print(f"vd: {vd}")
    print(f"network: {network}")
