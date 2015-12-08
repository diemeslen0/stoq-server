# -*- coding: utf-8 -*-
# vi:si:et:sw=4:sts=4:ts=4

##
## Copyright (C) 2015 Async Open Source <http://www.async.com.br>
## All rights reserved
##
## This program is free software; you can redistribute it and/or
## modify it under the terms of the GNU Lesser General Public License
## as published by the Free Software Foundation; either version 2
## of the License, or (at your option) any later version.
##
## This program is distributed in the hope that it will be useful,
## but WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU Lesser General Public License for more details.
##
## You should have received a copy of the GNU Lesser General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., or visit: http://www.gnu.org/.
##
## Author(s): Stoq Team <stoq-devel@async.com.br>
##

import io
import logging
import multiprocessing
import os
import signal
import sys

from stoqlib.lib.pluginmanager import get_plugin_manager
from stoqlib.lib.configparser import get_config
from htsql.core.fmt.emit import emit
from htsql.core.error import Error as HTSQL_Error
from htsql import HTSQL

from stoqserver.tasks import (backup_status, restore_database,
                              start_xmlrpc_server, start_server,
                              start_backup_scheduler)

logger = logging.getLogger(__name__)


class _Task(multiprocessing.Process):

    def __init__(self, func, *args, **kwargs):
        super(_Task, self).__init__()

        self.func = func
        self._func_args = args
        self._func_kwargs = kwargs
        self.daemon = True

    #
    #  multiprocessing.Process
    #

    def run(self):
        self.func(*self._func_args, **self._func_kwargs)


class TaskManager(object):

    def __init__(self):
        self._conn1, self._conn2 = multiprocessing.Pipe(True)

    #
    #  Public API
    #

    def run(self):
        self._start_tasks()
        while True:
            action = self._conn1.recv()
            meth = getattr(self, 'action_' + action[0])
            assert meth, "Action handler for %s not found" % (action[0], )
            self._conn1.send(meth(*action[1:]))

    def stop(self, close_xmlrpc=False):
        for p in multiprocessing.active_children():
            if not close_xmlrpc and p.func is start_xmlrpc_server:
                continue
            if not p.is_alive():
                continue

            os.kill(p.pid, signal.SIGTERM)
            # Give it 2 seconds to exit. If that doesn't happen, force
            # its termination
            p.join(2)
            if p.is_alive():
                p.terminate()

    #
    #  Actions
    #

    def action_restart(self):
        self.stop(close_xmlrpc=True)
        # execv will restart the process and finish this one
        os.execv(sys.argv[0], sys.argv)

    def action_htsql_query(self, query):
        """Executes a HTSQL Query"""
        # Resolve RDBMSs to their respective HTSQL engines
        engines = {
            'postgres': 'pgsql',
            'sqlite': 'sqlite',
            'mysql': 'mysql',
            'oracle': 'oracle',
            'mssql': 'mssql',
        }

        # Get the config data
        config = get_config()
        config = {
            'rdbms': engines[config.get('Database', 'rdbms')],
            'address': config.get('Database', 'address'),
            'port': config.get('Database', 'port'),
            'dbname': config.get('Database', 'dbname'),
            'dbusername': config.get('Database', 'dbusername'),
        }

        uri = '{rdbms}://{dbusername}@{address}:{port}/{dbname}'
        store = HTSQL(uri.format(**config))
        try:
            rows = store.produce(query)
        except HTSQL_Error as e:
            return False, str(e)

        with store:
            json = ''.join(emit('x-htsql/json', rows))

        return True, json

    def action_backup_status(self, user_hash=None):
        with io.StringIO() as f:
            duplicity_log = logging.getLogger("duplicity")
            handler = logging.StreamHandler(f)
            duplicity_log.addHandler(handler)

            try:
                backup_status(user_hash=user_hash)
            except Exception as e:
                retval = False
                msg = str(e)
            else:
                retval = True
                msg = f.getvalue()

            duplicity_log.removeHandler(handler)

        return retval, msg

    def action_backup_restore(self, user_hash, time=None):
        self.stop()

        try:
            restore_database(user_hash=user_hash, time=time)
        except Exception as e:
            retval = False
            msg = str(e)
        else:
            retval = True
            msg = "Restore finished"

        self._start_tasks()
        return retval, msg

    #
    #  Private
    #

    def _start_tasks(self):
        tasks = [
            _Task(start_backup_scheduler),
            _Task(start_server),
        ]
        if start_xmlrpc_server not in [t.func for t in
                                       multiprocessing.active_children()]:
            tasks.append(_Task(start_xmlrpc_server, self._conn2))

        manager = get_plugin_manager()
        for plugin_name in manager.installed_plugins_names:
            plugin = manager.get_plugin(plugin_name)
            if not hasattr(plugin, 'get_server_tasks'):
                continue
            tasks.extend(_Task(t) for t in plugin.get_server_tasks())

        for t in tasks:
            t.start()