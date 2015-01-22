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

import os

from cloudinit import log as logging
from cloudinit import sources
from cloudinit import util
from cloudinit import url_helper
 
LOG = logging.getLogger(__name__)
SOFTLAYER_API_QUERY_URL = 'https://api.service.softlayer.com/rest/v3.1/SoftLayer_Resource_Metadata'

class DataSourceSoftLayer(sources.DataSource):
    def __init__(self, sys_cfg, distro, paths):
        sources.DataSource.__init__(self, sys_cfg, distro, paths)
        self.seed_dir = os.path.join(paths.seed_dir, 'sl')
        self.metadata = {}
        self.userdata_raw = None

    def _fetch_metadata_item(self, item):
        item_url = url_helper.combine_url(SOFTLAYER_API_QUERY_URL, item)
        resp = url_helper.readurl(url=item_url)
        if resp.code == 200:
            return resp.contents
        return None

    def get_data(self):
        try:
            self.metadata['public_fqdn'] = self._fetch_metadata_item("FullyQualifiedDomainName.txt")
            self.metadata['hostname']    = self._fetch_metadata_item("Hostname.txt")
            self.metadata['instance-id'] = self._fetch_metadata_item("Id.txt")
            self.userdata_raw = self._fetch_metadata_item("UserMetadata.txt")
            return True
        except:
            return False

    def get_hostname(self, fqdn=False):
        if fqdn:
            return self.metadata['public_fqdn']
        return self.metadata['hostname']

# Used to match classes to dependencies
datasources = [ (DataSourceSoftLayer, (sources.DEP_FILESYSTEM, sources.DEP_NETWORK)), ]
# Return a list of data sources that match this set of dependencies
def get_datasource_list(depends):
    return sources.list_from_depends(depends, datasources)
