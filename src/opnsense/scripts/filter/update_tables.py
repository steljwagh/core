#!/usr/local/bin/python3

"""
    Copyright (c) 2017-2022 Ad Schellevis <ad@opnsense.org>
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    1. Redistributions of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.

    2. Redistributions in binary form must reproduce the above copyright
     notice, this list of conditions and the following disclaimer in the
     documentation and/or other materials provided with the distribution.

    THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES,
    INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
    AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
    AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY,
    OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.

    --------------------------------------------------------------------------------------
    update aliases
"""

import os
import sys
import argparse
import json
import urllib3
import xml.etree.cElementTree as ET
import syslog
import glob
from lib.alias import AliasParser
from lib.pf import PF
import lib.geoip as geoip
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)



if __name__ == '__main__':
    result = {'status': 'ok'}
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', help='output type [json/text]', default='json')
    parser.add_argument('--source_conf', help='configuration xml', default='/usr/local/etc/filter_tables.conf')
    inputargs = parser.parse_args()
    syslog.openlog('firewall', facility=syslog.LOG_LOCAL4)
    # make sure our target directory exists
    if not os.path.isdir('/var/db/aliastables'):
        os.makedirs('/var/db/aliastables')

    # make sure we download geoip data if not found. Since aliases only will trigger a download when change requires it
    if not os.path.isfile('/usr/local/share/GeoIP/alias.stats'):
        geoip.download_geolite()

    try:
        source_tree = ET.ElementTree(file=inputargs.source_conf)
    except ET.ParseError as e:
        syslog.syslog(syslog.LOG_ERR, 'filter table parse error (%s) %s' % (str(e), inputargs.source_conf))
        sys.exit(-1)

    aliases = AliasParser(source_tree)
    aliases.read()

    registered_aliases = set()
    for alias in aliases:
        # fetch alias content including dependencies
        alias_name = alias.get_name()
        alias_content = alias.resolve()
        alias_changed_or_expired = max(alias.changed(), alias.expired())
        for related_alias_name in aliases.get_alias_deps(alias_name):
            if related_alias_name != alias_name:
                rel_alias = aliases.get(related_alias_name)
                if rel_alias:
                    alias_changed_or_expired = max(alias_changed_or_expired, rel_alias.changed(), rel_alias.expired())
                    alias_content += rel_alias.resolve()

        if alias.get_parser():
            registered_aliases.add(alias.get_name())

            # when the alias or any of it's dependencies has changed, generate new
            if alias_changed_or_expired or not os.path.isfile('/var/db/aliastables/%s.txt' % alias_name):
                open('/var/db/aliastables/%s.txt' % alias_name, 'w').write('\n'.join(sorted(alias_content)))

            # only try to replace the contents of this alias if we're responsible for it (know how to parse)
            alias_pf_content = list(PF.list_table(alias_name))

            if (len(alias_content) != len(alias_pf_content) or alias_changed_or_expired):
                # if the alias is changed, expired or the one in memory has a different number of items, load table
                if len(alias_content) == 0:
                    # flush when target is empty
                    PF.flush(alias_name)
                else:
                    # replace table contents with collected alias
                    error_output = PF.replace(alias_name, '/var/db/aliastables/%s.txt' % alias_name)
                    if error_output.find('pfctl: ') > -1:
                        error_message = "Error loading alias [%s]: %s {current_size: %d, new_size: %d}" % (
                            alias_name,
                            error_output.replace('pfctl: ', ''),
                            len(alias_pf_content),
                            len(alias_content),
                        )
                        result['status'] = 'error'
                        if 'messages' not in result:
                            result['messages'] = list()
                        if error_output not in result['messages']:
                            result['messages'].append(error_message)
                            syslog.syslog(syslog.LOG_NOTICE, error_message)

    # cleanup removed aliases
    to_remove = dict()
    for filename in glob.glob('/var/db/aliastables/*.txt'):
        aliasname = os.path.basename(filename).split('.')[0]
        if aliasname not in registered_aliases:
            if aliasname not in to_remove:
                to_remove[aliasname] = list()
            to_remove[aliasname].append(filename)
    for aliasname in to_remove:
        syslog.syslog(syslog.LOG_NOTICE, 'remove old alias %s' % aliasname)
        PF.remove(aliasname)
        for filename in to_remove[aliasname]:
            os.remove(filename)

    print (json.dumps(result))
