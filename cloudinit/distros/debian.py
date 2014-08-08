# vi: ts=4 expandtab
#
#    Copyright (C) 2012 Canonical Ltd.
#    Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
#    Copyright (C) 2012 Yahoo! Inc.
#
#    Author: Scott Moser <scott.moser@canonical.com>
#    Author: Juerg Haefliger <juerg.haefliger@hp.com>
#    Author: Joshua Harlow <harlowja@yahoo-inc.com>
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

from cloudinit import distros
from cloudinit import helpers
from cloudinit import log as logging
from cloudinit import util

from cloudinit.distros.parsers.hostname import HostnameConf
from cloudinit.distros.net_util import NetConfHelper
from cloudinit.settings import PER_INSTANCE

LOG = logging.getLogger(__name__)

APT_GET_COMMAND = ('apt-get', '--option=Dpkg::Options::=--force-confold',
                   '--option=Dpkg::options::=--force-unsafe-io',
                   '--assume-yes', '--quiet')
APT_GET_WRAPPER = {
    'command': 'eatmydata',
    'enabled': 'auto',
}


class Distro(distros.Distro):
    hostname_conf_fn = "/etc/hostname"
    locale_conf_fn = "/etc/default/locale"
    network_conf_fn = "/etc/network/interfaces"

    def __init__(self, name, cfg, paths):
        distros.Distro.__init__(self, name, cfg, paths)
        # This will be used to restrict certain
        # calls from repeatly happening (when they
        # should only happen say once per instance...)
        self._runner = helpers.Runners(paths)
        self.osfamily = 'debian'

    def apply_locale(self, locale, out_fn=None):
        if not out_fn:
            out_fn = self.locale_conf_fn
        util.subp(['locale-gen', locale], capture=False)
        util.subp(['update-locale', locale], capture=False)
        # "" provides trailing newline during join
        lines = [
            util.make_header(),
            'LANG="%s"' % (locale),
            "",
        ]
        util.write_file(out_fn, "\n".join(lines))

    def install_packages(self, pkglist):
        self.update_package_sources()
        self.package_command('install', pkgs=pkglist)

    def _debian_network_json(self, settings):
        nc = NetConfHelper(settings)
        lines = []

        lines.append("# Created by cloud-init on instance boot.")
        lines.append("#")
        lines.append("# This file describes the network interfaces available on your system")
        lines.append("# and how to activate them. For more information, see interfaces(5).")
        lines.append("")
        lines.append("# The loopback network interface")
        lines.append("auto lo")
        lines.append("iface lo inet loopback")
        lines.append("")

        bonds = nc.get_links_by_type('bond')
        for bond in bonds:
            chunk = []
            chunk.append("auto {0}".format(bond['id']))
            chunk.append("iface {0} inet manual".format(bond['id']))
            chunk.append('  bond-mode {0}'.format(bond['bond_mode']))
            slaves = [nc.get_link_devname(nc.get_link_by_name(x)) for x in bond['bond_links']]
            chunk.append('  bond-slaves {0}'.format(' '.join(slaves)))
            chunk.append("")
            lines.extend(chunk)

        networks = nc.get_networks()
        for net in networks:
            # only have support for ipv4 so far.
            if net['type'] != "ipv4":
                continue

            link = nc.get_link_by_name(net['link'])
            devname = nc.get_link_devname(link)
            chunk = []
            chunk.append("# network: {0}".format(net['id']))
            chunk.append("# neutron_network_id: {0}".format(net['neutron_network_id']))
            chunk.append("auto {0}".format(devname))
            chunk.append("iface {0} inet static".format(devname))
            if link['type'] == "vlan":
                chunk.append("  vlan_raw_device {0}".format(devname[:devname.rfind('.')]))
                chunk.append("  hwaddress ether {0}".format(link['ethernet_mac_address']))
            chunk.append("  address {0}".format(net['ip_address']))
            chunk.append("  netmask {0}".format(net['netmask']))
            gwroute = [route for route in net['routes'] if route['network'] == '0.0.0.0']
            # TODO: hmmm
            if len(gwroute) == 1:
                chunk.append("  gateway {0}".format(gwroute[0]['gateway']))

            for route in net['routes']:
                if route['network'] == '0.0.0.0':
                    continue
                chunk.append("  post-up route add -net {0} netmask {1} gw {2} || true".format(route['network'],
                    route['netmask'], route['gateway']))
                chunk.append("  pre-down route del -net {0} netmask {1} gw {2} || true".format(route['network'],
                    route['netmask'], route['gateway']))
            chunk.append("")
            lines.extend(chunk)
        return {'/etc/network/interfaces': "\n".join(lines)}

    def _write_network_json(self, settings):
        files = self._rhel_network_json(settings)
        for (fn, data) in files.iteritems():
            util.write_file(fn, data)
        return ['all']

    def _write_network(self, settings):
        util.write_file(self.network_conf_fn, settings)
        return ['all']

    def _bring_up_interfaces(self, device_names):
        use_all = False
        for d in device_names:
            if d == 'all':
                use_all = True
        if use_all:
            return distros.Distro._bring_up_interface(self, '--all')
        else:
            return distros.Distro._bring_up_interfaces(self, device_names)

    def _select_hostname(self, hostname, fqdn):
        # Prefer the short hostname over the long
        # fully qualified domain name
        if not hostname:
            return fqdn
        return hostname

    def _write_hostname(self, your_hostname, out_fn):
        conf = None
        try:
            # Try to update the previous one
            # so lets see if we can read it first.
            conf = self._read_hostname_conf(out_fn)
        except IOError:
            pass
        if not conf:
            conf = HostnameConf('')
        conf.set_hostname(your_hostname)
        util.write_file(out_fn, str(conf), 0644)

    def _read_system_hostname(self):
        sys_hostname = self._read_hostname(self.hostname_conf_fn)
        return (self.hostname_conf_fn, sys_hostname)

    def _read_hostname_conf(self, filename):
        conf = HostnameConf(util.load_file(filename))
        conf.parse()
        return conf

    def _read_hostname(self, filename, default=None):
        hostname = None
        try:
            conf = self._read_hostname_conf(filename)
            hostname = conf.hostname
        except IOError:
            pass
        if not hostname:
            return default
        return hostname

    def _get_localhost_ip(self):
        # Note: http://www.leonardoborda.com/blog/127-0-1-1-ubuntu-debian/
        return "127.0.1.1"

    def set_timezone(self, tz):
        set_etc_timezone(tz=tz, tz_file=self._find_tz_file(tz))

    def package_command(self, command, args=None, pkgs=None):
        if pkgs is None:
            pkgs = []

        e = os.environ.copy()
        # See: http://tiny.cc/kg91fw
        # Or: http://tiny.cc/mh91fw
        e['DEBIAN_FRONTEND'] = 'noninteractive'

        wcfg = self.get_option("apt_get_wrapper", APT_GET_WRAPPER)
        cmd = _get_wrapper_prefix(
            wcfg.get('command', APT_GET_WRAPPER['command']),
            wcfg.get('enabled', APT_GET_WRAPPER['enabled']))

        cmd.extend(list(self.get_option("apt_get_command", APT_GET_COMMAND)))

        if args and isinstance(args, str):
            cmd.append(args)
        elif args and isinstance(args, list):
            cmd.extend(args)

        subcmd = command
        if command == "upgrade":
            subcmd = self.get_option("apt_get_upgrade_subcommand",
                                     "dist-upgrade")

        cmd.append(subcmd)

        pkglist = util.expand_package_list('%s=%s', pkgs)
        cmd.extend(pkglist)

        # Allow the output of this to flow outwards (ie not be captured)
        util.log_time(logfunc=LOG.debug,
            msg="apt-%s [%s]" % (command, ' '.join(cmd)), func=util.subp,
            args=(cmd,), kwargs={'env': e, 'capture': False})

    def update_package_sources(self):
        self._runner.run("update-sources", self.package_command,
                         ["update"], freq=PER_INSTANCE)

    def get_primary_arch(self):
        (arch, _err) = util.subp(['dpkg', '--print-architecture'])
        return str(arch).strip()


def _get_wrapper_prefix(cmd, mode):
    if isinstance(cmd, str):
        cmd = [str(cmd)]

    if (util.is_true(mode) or
        (str(mode).lower() == "auto" and cmd[0] and
         util.which(cmd[0]))):
        return cmd
    else:
        return []
