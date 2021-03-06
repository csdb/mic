#!/usr/bin/python -tt
#
# Copyright (c) 2011 Intel, Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; version 2 of the License
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 59
# Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os
import ConfigParser

import msger
import kickstart
from .utils import misc, runner, proxy, errors

DEFAULT_GSITECONF = '/etc/mic/mic.conf'

class ConfigMgr(object):
    DEFAULTS = {'common': {
                    "distro_name": "Default Distribution",
                },
                'create': {
                    "tmpdir": '/var/tmp/mic',
                    "cachedir": '/var/tmp/mic/cache',
                    "outdir": './mic-output',
                    "bootstrapdir": '/var/tmp/mic/bootstrap',

                    "arch": None, # None means auto-detect
                    "pkgmgr": "yum",
                    "name": "output",
                    "ksfile": None,
                    "ks": None,
                    "repomd": None,
                    "local_pkgs_path": None,
                    "release": None,
                    "logfile": None,
                    "record_pkgs": [],
                    "rpmver": None,
                    "compress_disk_image": None,
                    "name_prefix": None,
                    "proxy": None,
                    "no_proxy": None,

                    "runtime": None,
                },
                'chroot': {},
                'convert': {},
                'bootstraps': {},
               }

    # make the manager class as singleton
    _instance = None
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ConfigMgr, cls).__new__(cls, *args, **kwargs)

        return cls._instance

    def __init__(self, ksconf=None, siteconf=None):
        # reset config options
        self.reset()

        # initial options from siteconf
        if siteconf:
            self._siteconf = siteconf
        else:
            # use default site config
            self._siteconf = DEFAULT_GSITECONF

        if ksconf:
            self._ksconf = ksconf

    def reset(self):
        self.__ksconf = None
        self.__siteconf = None

        # initialize the values with defaults
        for sec, vals in self.DEFAULTS.iteritems():
            setattr(self, sec, vals)

    def __set_siteconf(self, siteconf):
        try:
            self.__siteconf = siteconf
            self._parse_siteconf(siteconf)
        except ConfigParser.Error, error:
            raise errors.ConfigError("%s" % error)
    def __get_siteconf(self):
        return self.__siteconf
    _siteconf = property(__get_siteconf, __set_siteconf)

    def __set_ksconf(self, ksconf):
        if not os.path.isfile(ksconf):
            msger.error('Cannot find ks file: %s' % ksconf)

        self.__ksconf = ksconf
        self._parse_kickstart(ksconf)
    def __get_ksconf(self):
        return self.__ksconf
    _ksconf = property(__get_ksconf, __set_ksconf)

    def _parse_siteconf(self, siteconf):
        if not siteconf:
            return

        if not os.path.exists(siteconf):
            raise errors.ConfigError("Failed to find config file: %s" \
                                     % siteconf)

        parser = ConfigParser.SafeConfigParser()
        parser.read(siteconf)

        for section in parser.sections():
            if section in self.DEFAULTS:
                getattr(self, section).update(dict(parser.items(section)))

        # append common section items to other sections
        for section in self.DEFAULTS.keys():
            if section != "common" and not section.startswith('bootstrap'):
                getattr(self, section).update(self.common)

        proxy.set_proxies(self.create['proxy'], self.create['no_proxy'])

        for section in parser.sections():
            if section.startswith('bootstrap'):
                name = section
                repostr = {}
                for option in parser.options(section):
                    if option == 'name':
                        name = parser.get(section, 'name')
                        continue

                    val = parser.get(section, option)
                    if '_' in option:
                        (reponame, repoopt) = option.split('_')
                        if repostr.has_key(reponame):
                            repostr[reponame] += "%s:%s," % (repoopt, val)
                        else:
                            repostr[reponame] = "%s:%s," % (repoopt, val)
                        continue

                    if val.split(':')[0] in ('file', 'http', 'https', 'ftp'):
                        if repostr.has_key(option):
                            repostr[option] += "name:%s,baseurl:%s," % (option, val)
                        else:
                            repostr[option]  = "name:%s,baseurl:%s," % (option, val)
                        continue

                self.bootstraps[name] = repostr

    def _selinux_check(self, arch, ks):
        """If a user needs to use btrfs or creates ARM image,
        selinux must be disabled at start.
        """

        for path in ["/usr/sbin/getenforce",
                     "/usr/bin/getenforce",
                     "/sbin/getenforce",
                     "/bin/getenforce",
                     "/usr/local/sbin/getenforce",
                     "/usr/locla/bin/getenforce"
                     ]:
            if os.path.exists(path):
                selinux_status = runner.outs([path])
                if arch and arch.startswith("arm") \
                        and selinux_status == "Enforcing":
                    raise errors.ConfigError("Can't create arm image if "
                          "selinux is enabled, please disable it and try again")

                use_btrfs = False
                for part in ks.handler.partition.partitions:
                    if part.fstype == "btrfs":
                        use_btrfs = True
                        break

                if use_btrfs and selinux_status == "Enforcing":
                    raise errors.ConfigError("Can't create image using btrfs "
                                           "filesystem if selinux is enabled, "
                                           "please disable it and try again.")
                break

    def _parse_kickstart(self, ksconf=None):
        if not ksconf:
            return

        ks = kickstart.read_kickstart(ksconf)

        self.create['ks'] = ks
        self.create['name'] = os.path.splitext(os.path.basename(ksconf))[0]

        if self.create['name_prefix']:
            self.create['name'] = "%s-%s" % (self.create['name_prefix'],
                                             self.create['name'])

        self._selinux_check (self.create['arch'], ks)

        msger.info("Retrieving repo metadata:")
        ksrepos = misc.get_repostrs_from_ks(ks)
        if not ksrepos:
            raise errors.KsError('no valid repos found in ks file')

        self.create['repomd'] = misc.get_metadata_from_repos(
                                                    ksrepos,
                                                    self.create['cachedir'])
        msger.raw(" DONE")

        self.create['rpmver'] = misc.get_rpmver_in_repo(self.create['repomd'])

        target_archlist, archlist = misc.get_arch(self.create['repomd'])
        if self.create['arch']:
            if self.create['arch'] not in archlist:
                raise errors.ConfigError("Invalid arch %s for repository. "
                                  "Valid arches: %s" \
                                  % (self.create['arch'], ', '.join(archlist)))
        else:
            if len(target_archlist) == 1:
                self.create['arch'] = str(target_archlist[0])
                msger.info("\nUse detected arch %s." % target_archlist[0])
            else:
                raise errors.ConfigError("Please specify a valid arch, "
                                         "the choice can be: %s" \
                                         % ', '.join(archlist))

        kickstart.resolve_groups(self.create, self.create['repomd'])

configmgr = ConfigMgr()
