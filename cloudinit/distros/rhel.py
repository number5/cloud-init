# vi: ts=4 expandtab
#
#    Copyright (C) 2012 Canonical Ltd.
#    Copyright (C) 2012, 2013 Hewlett-Packard Development Company, L.P.
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

from cloudinit import distros
from cloudinit import helpers
from cloudinit import log as logging
from cloudinit import util

from cloudinit.distros import rhel_util
from cloudinit.settings import PER_INSTANCE

LOG = logging.getLogger(__name__)


def _make_sysconfig_bool(val):
    if val:
        return 'yes'
    else:
        return 'no'


class Distro(distros.Distro):
    # See: http://tiny.cc/6r99fw
    clock_conf_fn = "/etc/sysconfig/clock"
    locale_conf_fn = '/etc/sysconfig/i18n'
    systemd_locale_conf_fn = '/etc/locale.conf'
    network_conf_fn = "/etc/sysconfig/network"
    hostname_conf_fn = "/etc/sysconfig/network"
    systemd_hostname_conf_fn = "/etc/hostname"
    network_script_tpl = '/etc/sysconfig/network-scripts/ifcfg-%s'
    resolve_conf_fn = "/etc/resolv.conf"
    tz_local_fn = "/etc/localtime"

    def __init__(self, name, cfg, paths):
        distros.Distro.__init__(self, name, cfg, paths)
        # This will be used to restrict certain
        # calls from repeatly happening (when they
        # should only happen say once per instance...)
        self._runner = helpers.Runners(paths)
        self.osfamily = 'redhat'

    def install_packages(self, pkglist):
        self.package_command('install', pkgs=pkglist)

    def _write_network(self, settings):
        # TODO(harlowja) fix this... since this is the ubuntu format
        entries = rhel_util.translate_network(settings)
        LOG.debug("Translated ubuntu style network settings %s into %s",
                  settings, entries)
        # Make the intermediate format as the rhel format...
        nameservers = []
        searchservers = []
        local_domain = None
        dev_names = entries.keys()
        for (dev, info) in entries.iteritems():
            net_fn = self.network_script_tpl % (dev)
            net_cfg = {
                'DEVICE': dev,
                'NETMASK': info.get('netmask'),
                'IPADDR': info.get('address'),
                'BOOTPROTO': info.get('bootproto'),
                'GATEWAY': info.get('gateway'),
                'BROADCAST': info.get('broadcast'),
                'MACADDR': info.get('hwaddress'),
                'ONBOOT': _make_sysconfig_bool(info.get('auto')),
            }
            rhel_util.update_sysconfig_file(net_fn, net_cfg)
            if 'dns-nameservers' in info:
                nameservers.extend(info['dns-nameservers'])
            if 'dns-search' in info:
                searchservers.extend(info['dns-search'])
            if 'dns-domain' in info:
                local_domain = info['dns-domain']
        if nameservers or searchservers or local_domain:
            rhel_util.update_resolve_conf_file(self.resolve_conf_fn,
                                               nameservers, searchservers, local_domain)
        if dev_names:
            net_cfg = {
                'NETWORKING': _make_sysconfig_bool(True),
            }
            rhel_util.update_sysconfig_file(self.network_conf_fn, net_cfg)
        return dev_names

    def _dist_uses_systemd(self):
        # Fedora 18 and RHEL 7 were the first adopters in their series
        (dist, vers) = util.system_info()['dist'][:2]
        major = (int)(vers.split('.')[0])
        return ((dist.startswith('Red Hat Enterprise Linux') and major >= 7)
                or (dist.startswith('Fedora') and major >= 18))

    def apply_locale(self, locale, out_fn=None):
        if self._dist_uses_systemd():
            if not out_fn:
                out_fn = self.systemd_locale_conf_fn
            out_fn = self.systemd_locale_conf_fn
        else:
            if not out_fn:
                out_fn = self.locale_conf_fn
        locale_cfg = {
            'LANG': locale,
        }
        rhel_util.update_sysconfig_file(out_fn, locale_cfg)

    def _write_hostname(self, hostname, out_fn):
        if self._dist_uses_systemd():
            util.subp(['hostnamectl', 'set-hostname', str(hostname)])
        else:
            host_cfg = {
                'HOSTNAME': hostname,
            }
            rhel_util.update_sysconfig_file(out_fn, host_cfg)

    def _select_hostname(self, hostname, fqdn):
        # See: http://bit.ly/TwitgL
        # Should be fqdn if we can use it
        if fqdn:
            return fqdn
        return hostname

    def _read_system_hostname(self):
        if self._dist_uses_systemd():
            host_fn = self.systemd_hostname_conf_fn
        else:
            host_fn = self.hostname_conf_fn
        return (host_fn, self._read_hostname(host_fn))

    def _read_hostname(self, filename, default=None):
        if self._dist_uses_systemd():
            (out, _err) = util.subp(['hostname'])
            if len(out):
                return out
            else:
                return default
        else:
            (_exists, contents) = rhel_util.read_sysconfig_file(filename)
            if 'HOSTNAME' in contents:
                return contents['HOSTNAME']
            else:
                return default

    def _bring_up_interfaces(self, device_names):
        if device_names and 'all' in device_names:
            raise RuntimeError(('Distro %s can not translate '
                                'the device name "all"') % (self.name))
        return distros.Distro._bring_up_interfaces(self, device_names)

    def set_timezone(self, tz):
        tz_file = self._find_tz_file(tz)
        if self._dist_uses_systemd():
            # Currently, timedatectl complains if invoked during startup
            # so for compatibility, create the link manually.
            util.del_file(self.tz_local_fn)
            util.sym_link(tz_file, self.tz_local_fn)
        else:
            # Adjust the sysconfig clock zone setting
            clock_cfg = {
                'ZONE': str(tz),
            }
            rhel_util.update_sysconfig_file(self.clock_conf_fn, clock_cfg)
            # This ensures that the correct tz will be used for the system
            util.copy(tz_file, self.tz_local_fn)

    def package_command(self, command, args=None, pkgs=None):
        if pkgs is None:
            pkgs = []

        cmd = ['yum']
        # If enabled, then yum will be tolerant of errors on the command line
        # with regard to packages.
        # For example: if you request to install foo, bar and baz and baz is
        # installed; yum won't error out complaining that baz is already
        # installed.
        cmd.append("-t")
        # Determines whether or not yum prompts for confirmation
        # of critical actions. We don't want to prompt...
        cmd.append("-y")

        if args and isinstance(args, str):
            cmd.append(args)
        elif args and isinstance(args, list):
            cmd.extend(args)

        cmd.append(command)

        pkglist = util.expand_package_list('%s-%s', pkgs)
        cmd.extend(pkglist)

        # Allow the output of this to flow outwards (ie not be captured)
        util.subp(cmd, capture=False)

    def update_package_sources(self):
        self._runner.run("update-sources", self.package_command,
                         ["makecache"], freq=PER_INSTANCE)
