# vi: ts=4 expandtab
#
#    Copyright (C) 2012 Canonical Ltd.
#    Copyright (C) 2012 Yahoo! Inc.
#
#    Author: Peter Schroeter <schroeter@gmail.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License version 3, as
#    published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import subprocess

from cloudinit import log as logging
from cloudinit import sources
from cloudinit import util

LOG = logging.getLogger(__name__)

# Various defaults/constants...
DS_NAME = 'ExtraConfig'

DEFAULT_IID = "iid-dsextraconfig"
DEFAULT_METADATA = {
    "instance-id": DEFAULT_IID,
}

# RightScale format for passing network config supported for now. Look into
# other formats (Openstack)
BUILTIN_DS_CONFIG = {
    "metadata_format": "rightscale"
}
DS_CFG_PATH = ['datasource', DS_NAME]

VMTOOLSD = "vmtoolsd"


class DataSourceExtraConfig(sources.DataSource):
    def __init__(self, sys_cfg, distro, paths):
        super(DataSourceExtraConfig, self).__init__(sys_cfg, distro, paths)
        self.cmdline_id = "ds=extraconfig"
        self.dsmode = "local"
        self.userdata_raw = None 
        self.metadata = {}
        self.ds_cfg = util.mergemanydict([
            util.get_cfg_by_path(sys_cfg, DS_CFG_PATH, {}),
            BUILTIN_DS_CONFIG])

    def fetch_extra_config(self, metadata_type):
        command = '--cmd=info-get guestinfo.' + metadata_type
        data = ""
        try:
            (data, err) = util.subp([VMTOOLSD, command])
        except OSError:
            raise NoVmwareToolsInstall("No %s command found" % VMTOOLSD)

        if data == "":
            raise NonExtraConfigDataSource("The extra config key guestinfo.%s contains no value" % metadata_type)
        return data

    def parse_metadata(self, raw_metadata, seperator="&"):
        hsh = {}
        for pair in raw_metadata.split(seperator):
            try:
                name, value = pair.split("=", 2)
                hsh[name.strip()] = value.strip()
            except ValueError:
                pass
        return hsh
        

    def __str__(self):
        mstr = "DataSourceExtraConfig"
        return mstr

    def get_data(self):
        found = False
        try:
            md_raw = self.fetch_extra_config('metadata')
            ud_raw = self.fetch_extra_config('userdata')
            found = True
        except NoVmwareToolsInstall:
            pass
        except NonExtraConfigDataSource:
            pass

        if not found:
            return False
        self.userdata_raw = ud_raw
        md = self.parse_metadata(md_raw)
        if self.ds_cfg.get('metadata_format') == "rightscale":
            md['instance-id'] = md['vs_instance_id']
        self.metadata = util.mergemanydict([md, DEFAULT_METADATA])
        return True


class NoVmwareToolsInstall(Exception):
    pass

class NonExtraConfigDataSource(Exception):
    pass

# class DataSourceExtraConfigNet(DataSourceExtraConfig):
#     def __init__(self, sys_cfg, distro, paths):
#         DataSourceExtraConfig.__init__(self, sys_cfg, distro, paths)
#         self.cmdline_id = "ds=extraconfig-net"
#         self.dsmode = "net"


# Used to match classes to dependencies
datasources = [
  (DataSourceExtraConfig, (sources.DEP_FILESYSTEM, sources.DEP_NETWORK)),
]


# Return a list of data sources that match this set of dependencies
def get_datasource_list(depends):
    return sources.list_from_depends(depends, datasources)
