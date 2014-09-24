# Copyright 2014 Scalyr Inc and the tcollector authors.
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.  This program is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser
# General Public License for more details.  You should have received a copy
# of the GNU Lesser General Public License along with this program.  If not,
# see <http://www.gnu.org/licenses/>.
#
# Note, this file draws heavily on the mysql collector distributed as part of
# the tcollector project (https://github.com/OpenTSDB/tcollector).  As such,
# this file is being distributed under GPLv3.
#
# Note, this can be run in standalone mode by:
#     python -m scalyr_agent.run_monitor scalyr_agent.builtin_monitors.mysql_monitor
import sys
import re
import os
import stat
import errno

from scalyr_agent import ScalyrMonitor, UnsupportedSystem

# We must require 2.6 or greater right now because PyMySQL requires it.  We are considering
# forking PyMySQL and adding in support if there is enough customer demand.
if sys.version_info[0] < 2 or (sys.version_info[0] == 2 and sys.version_info[1] < 6):
    raise UnsupportedSystem('mysql_monitor', 'Requires Python 2.6 or greater.')

# We import pymysql from the third_party directory.  This
# relies on PYTHONPATH being set up correctly, which is done
# in both agent_main.py and config_main.py
#
# noinspection PyUnresolvedReferences,PyPackageRequirements
import pymysql


def file_exists(path):
    if path is None:
        return False
    return os.path.exists(path)


def isyes(s):
    if s.lower() == "yes":
        return 1
    return 0


# We do not report any global status metrics that begin with 'com_' unless it is listed here.
__coms_to_report__ = ('com_select', 'com_update', 'com_delete', 'com_replace', 'com_insert')

class MysqlDB(object):
    """ Represents a MySQL database
    """
    
    def _connect(self):
        try:
            if self._type == "socket":
                conn = pymysql.connect (unix_socket = self._sockfile,
                                        user = self._username,
                                        passwd = self._password)
            else:
                conn = pymysql.connect (host = self._host,
                                        port = self._port,
                                        user = self._username,
                                        passwd = self._password)
            self._db = conn
            self._cursor = self._db.cursor()
            self._gather_db_information()                                                   
        except pymysql.Error, me:
            self._db = None
            self._cursor = None
            self._logger.error("Database connect failed: %s" % me)
        except Exception, ex:
            self._logger.error("Exception trying to connect occured:  %s" % ex)
            raise Exception("Exception trying to connect:  %s" % ex)
        
    def _close(self):
        """Closes the cursor and connection to this MySQL server."""
        if self._cursor:
            self._cursor.close()
        if self._db:
            self._db.close()
            
    def _reconnect(self):
        """Reconnects to this MySQL server."""
        self._close()
        self._connect()
        
    def _isShowGlobalStatusSafe(self):
        """Returns whether or not SHOW GLOBAL STATUS is safe to run."""
        # We can't run SHOW GLOBAL STATUS on versions prior to 5.1 because it
        # locks the entire database for too long and severely impacts traffic.
        return self._major > 5 or (self._major == 5 and self._medium >= 1)

    def _query(self, sql):
        """Executes the given SQL statement and returns a sequence of rows."""
        try:
            self._cursor.execute(sql)
        except pymysql.OperationalError, (errcode, msg):
            if errcode != 2006:  # "MySQL server has gone away"
                raise Exception("Database error -- " + errcode)
            self._reconnect()
            return None
        return self._cursor.fetchall()        
        
    def _gather_db_information(self):
        try:
            r = self._query("SELECT VERSION()")
            if r == None or len(r) == 0:
                self._version = "unknown"
            else:
                self._version = r[0][0]
            version = self._version.split(".")
            self._major = int(version[0])
            self._medium = int(version[1])
        except (ValueError, IndexError), e:
            self._major = self._medium = 0
            self._version = "unknown"
        except:
            ex = sys.exc_info()[0]
            self._logger.error("Exception getting database version: %s" % ex)
            self._version = "unknown"
            
    def gather_global_status(self):
        if not self._isShowGlobalStatusSafe():
            return None
        result = []
        for metric, value in self._query("SHOW /*!50000 GLOBAL */ STATUS"):
            value = self._parse_value(value)
            metric_name = metric.lower()
            # Do not include com_ counters except for com_select, com_delete, com_replace, com_insert, com_update
            if not metric_name.startswith('com_') or metric_name in __coms_to_report__:
                result.append( { "field": "global.%s" % metric_name, "value": value } )
        return result

    def gather_global_variables(self):
        result = []
        for metric, value in self._query("SHOW /*!50000 GLOBAL */ VARIABLES"):
            value = self._parse_value(value)
            metric_name = metric.lower()
            # To reduce the amount of metrics we write, we only include the variables that
            # we actually need.
            if metric_name == 'max_connections' or metric_name == 'open_files_limit':
                result.append( { "field": "vars.%s" % metric_name, "value": value } )
        return result
    
    def _has_innodb(self, globalStatus):
        if globalStatus is None:
            return False
        for k in globalStatus:
            if k['field'].startswith("global.innodb"):
                return True
        return False
        
    def _parse_value(self, value):
        try:
            if "." in value:
                value = float(value)
            else:
                value = int(value)
        except ValueError:
            value = str(value) # string values are possible
        return value
        
    def _parse_data(self, data, fields):
        result = []
        def match(regexp, line):
            return re.match(regexp, line)
        for line in data.split("\n"):
            for search in fields:
                m = match(search["regex"], line)
                if m:
                    for field in search["fields"]:
                        r = {
                            "field":    field["label"],
                            "value":    self._parse_value(m.group(field["group"]))
                        }
                        if "extra fields" in field:
                            r["extra fields"] = field["extra fields"]
                        result.append(r)
                    continue
        return result
        
    def gather_innodb_status(self, globalStatus):
        if not self._has_innodb(globalStatus):
            return None
            
        innodb_status  = self._query("SHOW ENGINE INNODB STATUS")[0][2]
        if innodb_status is None:
            return None
            
        innodb_queries = [
            { 
                "regex": "OS WAIT ARRAY INFO: reservation count (\d+), signal count (\d+)",
                "fields": [
                    {
                        "label": "innodb.oswait_array.reservation_count",
                        "group": 1
                    },
                    {
                        "label": "innodb.oswait_array.signal_count", 
                        "group": 2
                    }
                ]
            },
            {
                "regex": "Mutex spin waits (\d+), rounds (\d+), OS waits (\d+)",
                "fields": [
                    {
                        "label": "innodb.locks.spin_waits",
                        "group": 1,
                        "extra fields": {
                            "type": "mutex"
                        }
                    },
                    {
                        "label": "innodb.locks.rounds",
                        "group": 2,
                        "extra fields": {
                            "type": "mutex"
                        }
                    },
                    {
                        "label": "innodb.locks.os_waits",
                        "group": 3,
                        "extra fields": {
                            "type": "mutex"
                        }
                    },
                ]
            },
            {
                "regex": "RW-shared spins (\d+), OS waits (\d+); RW-excl spins (\d+), OS waits (\d+)",
                "fields": [
                    {
                        "label": "innodb.locks.spin_waits",
                        "group": 1,
                        "extra fields": {
                            "type": "rw-shared"
                        }
                    },
                    {
                        "label": "innodb.locks.os_waits",
                        "group": 2,
                        "extra fields": {
                            "type": "rw-shared"
                        }
                    },
                    {
                        "label": "innodb.locks.spin_waits",
                        "group": 3,
                        "extra fields": {
                            "type": "rw-exclusive"
                        }
                    },
                    {
                        "label": "innodb.locks.os_waits",
                        "group": 4,
                        "extra fields": {
                            "type": "rw-exclusive"
                        }
                    },                    
                ]
            },
            { 
                "regex": "Ibuf: size (\d+), free list len (\d+), seg size (\d+),",
                "fields": [
                    {
                        "label": "innodb.ibuf.size",
                        "group": 1
                    },
                    {
                        "label": "innodb.ibuf.free_list_len", 
                        "group": 2
                    },
                    {
                        "label": "innodb.ibuf.seg_size", 
                        "group": 3
                    }                    
                ]
            },            
            { 
                "regex": "(\d+) inserts, (\d+) merged recs, (\d+) merges",
                "fields": [
                    {
                        "label": "innodb.ibuf.inserts",
                        "group": 1
                    },
                    {
                        "label": "innodb.ibuf.merged_recs", 
                        "group": 2
                    },
                    {
                        "label": "innodb.ibuf.merges", 
                        "group": 3
                    }                    
                ]
            },            
            { 
                "regex": "\d+ queries inside InnoDB, (\d+) queries in queue",
                "fields": [
                    {
                        "label": "innodb.queries_queued",
                        "group": 1
                    }            
                ]
            },            
            { 
                "regex": "(\d+) read views open inside InnoDB",
                "fields": [
                    {
                        "label": "innodb.opened_read_views",
                        "group": 1
                    }            
                ]
            },            
            { 
                "regex": "History list length (\d+)",
                "fields": [
                    {
                        "label": "innodb.history_list_length",
                        "group": 1
                    }            
                ]
            },
        ]
        
        return self._parse_data(innodb_status, innodb_queries)
        
    def _row_to_dict(self, row):
        """Transforms a row (returned by DB.query) into a dict keyed by column names.

        db: The DB instance from which this row was obtained.
        row: A row as returned by DB.query
        """
        d = {}
        for i, field in enumerate(self._cursor.description):
            column = field[0].lower()  # Lower-case to normalize field names.
            d[column] = row[i]
        return d
        
    def gather_cluster_status(self):
        slave_status = self._query("SHOW SLAVE STATUS")
        if not slave_status:
            return None
        result = None    
        slave_status = self._row_to_dict(slave_status[0])
        if "master_host" in slave_status:
            master_host = slave_status["master_host"]
        else:
            master_host = None
        if master_host and master_host is not "None":
            result = []
            sbm = slave_status.get("seconds_behind_master")
            if isinstance(sbm, (int, long)):
                result.append( { "field": "slave.seconds_behind_master", "value": sbm })
            result.append( { "field": "slave.bytes_executed", "value": slave_status["exec_master_log_pos"] } )                
            result.append( { "field": "slave.bytes_relayed", "value": slave_status["read_master_log_pos"] } )                
            result.append( { "field": "slave.thread_io_running", "value": isyes(slave_status["slave_io_running"]) } )
            result.append( { "field": "slave.thread_sql_running", "value": isyes(slave_status["slave_sql_running"]) } )
        return result      
        
    def gather_process_information(self):
        result = []
        states = {}
        process_status = self._query("SHOW PROCESSLIST")
        for row in process_status:
            id, user, host, db_, cmd, time, state = row[:7]
            states[cmd] = states.get(cmd, 0) + 1
        for state, count in states.iteritems():
            state = state.lower().replace(" ", "_")
            result.append( { "field": "process.%s" % state, "value": count } )
        if len(result) == 0:
            result = None
        return result
        
    def _derived_stat_slow_query_percentage(self, globalVars, globalStatusMap):
        """Calculate the percentage of queries that are slow."""
        pct = 0.0
        if globalStatusMap['global.questions'] > 0:
            pct = 100.0 * (float(globalStatusMap['global.slow_queries']) / float(globalStatusMap['global.questions']))
        return pct
        
    def _derived_stat_connections_used_percentage(self, globalVars, globalStatusMap):
        """Calculate what percentage of the configured connections are used.  A high percentage can
           indicate a an app is using more than the expected number / configured number of connections.
        """
        pct = 100.0 * (float(globalStatusMap['global.max_used_connections']) / float(globalVars['vars.max_connections']))
        if pct > 100:
            pct = 100.0
        return pct
        
    def _derived_stat_aborted_clients_percentage(self, globalVars, globalStatusMap):
        """Calculate the percentage of client connection attempts that are aborted."""
        pct = 0.0
        if globalStatusMap['global.connections'] > 0:
            pct = 100.0 * (float(globalStatusMap['global.aborted_clients']) / float(globalStatusMap['global.connections']))
        return pct

    def _derived_stat_aborted_connections_percentage(self, globalVars, globalStatusMap):
        """Calculate the percentage of client connection attempts that fail."""
        pct = 0.0
        if globalStatusMap['global.connections'] > 0:
            pct = 100.0 * (float(globalStatusMap['global.aborted_connects']) / float(globalStatusMap['global.connections']))
        return pct

    def _derived_stat_read_write_percentage(self, globalVars, globalStatusMap, doRead):
        reads = globalStatusMap['global.com_select']
        writes = globalStatusMap['global.com_delete'] + globalStatusMap['global.com_insert'] + globalStatusMap['global.com_update'] + globalStatusMap['global.com_replace']
        pct = 0.0
        top = writes
        if doRead:
            top = reads
        if reads + writes > 0:
            pct = 100.0 * (float(top) / float(writes + reads))
        return pct
        
    def _derived_stat_write_percentage(self, globalVars, globalStatusMap):
        """Calculate the percentate of queries that are writes."""
        return self._derived_stat_read_write_percentage(globalVars, globalStatusMap, False)

    def _derived_stat_read_percentage(self, globalVars, globalStatusMap):
        """Calculate the percentate of queries that are reads."""
        return self._derived_stat_read_write_percentage(globalVars, globalStatusMap, True)
        
    def _derived_stat_query_cache_efficiency(self, globalVars, globalStatusMap):
        """How efficiently is the query cache being used?"""
        pct = 0.0
        if globalStatusMap['global.com_select'] + globalStatusMap['global.qcache_hits'] > 0:
            pct = 100.0 * (float(globalStatusMap['global.qcache_hits']) / float(globalStatusMap['global.com_select'] + globalStatusMap['global.qcache_hits']))
        return pct
        
    def _derived_stat_joins_without_indexes(self, globalVars, globalStatusMap):
        """Calculate the percentage of joins being done without indexes"""
        return globalStatusMap['global.select_range_check'] + globalStatusMap['global.select_full_join']
        
    def _derived_stat_table_cache_hit_rate(self, globalVars, globalStatusMap):
        """Calculate the percentage of table requests that are cached."""
        pct = 100.0
        if globalStatusMap['global.opened_tables'] > 0:
            pct = 100.0 * (float(globalStatusMap['global.open_tables']) / float(globalStatusMap['global.opened_tables']))
        return pct
        
    def _derived_stat_open_file_percentage(self, globalVars, globalStatusMap):
        """Calculate the percentage of files that are open compared to the allowed limit.
           If no open file limit is configured, the value will be 0.
        """
        pct = 0.0
        if globalVars['vars.open_files_limit'] > 0:
            pct = 100.0 * (float(globalStatusMap['global.open_files']) / float(globalVars['vars.open_files_limit']))
        return pct
    
    def _derived_stat_immediate_table_lock_percentage(self, globalVars, globalStatusMap):
        """Calculate how often a request to lock a table succeeds immediately."""
        pct = 100.0
        if globalStatusMap['global.table_locks_waited'] > 0:
            pct = 100.0 * (float(globalStatusMap['global.table_locks_immediate']) / float(globalStatusMap['global.table_locks_waited'] + globalStatusMap['global.table_locks_immediate']))
        return pct
                
    def _derived_stat_thread_cache_hit_rate(self, globalVars, globalStatusMap):
        """Calculate how regularly a connection comes in and a thread is available."""
        pct = 100.0
        if globalStatusMap['global.connections'] > 0:
            pct = 100.0 - (float(globalStatusMap['global.threads_created']) / float(globalStatusMap['global.connections']))
        return pct
        
    def _derived_stat_tmp_disk_table_percentage(self, globalVars, globalStatusMap):
        """Calculate the percentage of internal temporary tables were created on disk."""
        pct = 0.0
        if globalStatusMap['global.created_tmp_tables'] > 0:
            pct = 100.0 * float(globalStatusMap['global.created_tmp_disk_tables']) / float(globalStatusMap['global.created_tmp_tables'])
        return pct
       
                
    def gather_derived_stats(self, globalVars, globalStatusMap):
        """Gather derived stats based on global variables and global status.
        """
        if not globalVars or not globalStatusMap:
            return None
        stats = [
            "slow_query_percentage",
            "connections_used_percentage",
            "aborted_connections_percentage",
            "aborted_clients_percentage",
            "read_percentage",
            "write_percentage",
            "query_cache_efficiency",
            "joins_without_indexes",
            "table_cache_hit_rate",
            "open_file_percentage",
            "immediate_table_lock_percentage",
            "thread_cache_hit_rate",
            "tmp_disk_table_percentage"
        ]
        result = []
        for s in stats:
            method = "_derived_stat_%s" % s
            if hasattr(self, method) and callable(getattr(self, method)):
                func = getattr(self, method)
                val = func(globalVars, globalStatusMap)
                result.append( { "field": "derived.%s" % s, "value": val } )
        return result
        
    def is_sockfile(self, path):
        """Returns whether or not the given path is a socket file."""
        try:
            s = os.stat(path)
        except OSError, (no, e):
            if no == errno.ENOENT:
                return False
            self._logger.error("warning: couldn't stat(%r): %s" % (path, e))
            return None
        return s.st_mode & stat.S_IFSOCK == stat.S_IFSOCK        
        
    def __str__(self):
        if self._type == "socket":
            return "DB(%r, %r)" % (self._sockfile, self._version)
        else:
            return "DB(%r:%r, %r)" % (self._host, self._port, self._version)
            
    def __repr__(self):
        return self.__str__()
   
    def __init__(self, type = "sockfile", sockfile = None, host = None, port = None, username = None, password = None, logger = None):
        """Constructor: handles both socket files as well as host/port connectivity.
    
        @param type: is the connection a "socket" or "host:port"
        @param sockfile: if socket connection, the location of the sockfile
        @param host: if host:port connection, the name of the host
        @param port: if host:port connection, the port to connect to
        @param username: username to connect with
        @param password: password to establish connection
        """
        self._default_socket_locations = [
            "/tmp/mysql.sock",                  # MySQL's own default.
            "/var/lib/mysql/mysql.sock",        # RH-type / RPM systems.
            "/var/run/mysqld/mysqld.sock",      # Debian-type systems.
        ]
        
        self._type = type
        self._username = username
        self._password = password
        self._logger = logger
        if self._logger is None:
            raise Exception("Logger required.")
        if type == "socket":
            # if no socket file specified, attempt to find one locally
            if sockfile is None:
                for sock in self._default_socket_locations:
                    if file_exists(sock):
                        if self.is_sockfile(sock):
                            sockfile = sock
                            break        
            else:
                if not self.is_sockfile(sockfile):
                    raise Exception("Specified socket file is not a socket: %s" % sockfile)  
            if sockfile is None:
                raise Exception("Socket file required.  Either one was not specified or the default can not be found.") 
            self._sockfile = sockfile
        elif type == "host:port":
            self._host = host
            self._port = port
        else:
            raise Exception('Unsupported database connection type.')

        self._connect()
        if self._db is None:
            raise Exception('Unable to connect to db')


class MysqlMonitor(ScalyrMonitor):
    """A Scalyr agent monitor that monitors mysql databases.
    """
    def _initialize(self):
        """Performs monitor-specific initialization.
        """

        # Useful instance variables:
        #   _sample_interval_secs:  The number of seconds between calls to gather_sample.
        #   _config:  The dict containing the configuration for this monitor instance as retrieved from configuration
        #             file.
        #   _logger:  The logger instance to report errors/warnings/etc.
        
        # determine how we are going to connect
        if "database_socket" in self._config and "database_hostport" in self._config:
            raise Exception("Either 'database_socket' or 'database_hostport' can be specified.  Not both.")
        elif "database_socket" in self._config:
            self._database_connect_type = "socket"
            if type(self._config["database_socket"]) is str or type(self._config["database_socket"]) is unicode:
                self._database_socket = self._config["database_socket"]
                if len(self._database_socket) == 0:
                    raise Exception("A value for 'database_socket' must be specified.  To use default value, use the string 'default'")
                elif self._database_socket.lower() == "default":
                    self._database_socket = None # this triggers the default case where we try and determine socket location
            else:
                raise Exception('database_socket specified must be either an empty string or the location of the socket file to use.')
        elif "database_hostport" in self._config:
            self._database_connect_type = "host:port"
            if type(self._config["database_hostport"]) is str or type(self._config["database_hostport"]) is unicode:
                hostport = self._config["database_hostport"]
                if len(hostport) == 0:
                    raise Exception("A value for 'database_hostport' must be specified.  To use default value, use the string 'default'")
                elif hostport.lower() == "default":                
                    self._database_host = "localhost"
                    self._database_port = 3306                
                else:
                    hostPortParts = hostport.split(":")
                    self._database_host = hostPortParts[0]
                    if len(hostPortParts) == 1:
                        self._database_port = 3306
                    elif len(hostPortParts) == 2:
                        try:
                            self._database_port = int(hostPortParts[1])
                        except:
                            raise Exception("database_hostport specified is incorrect.  The format show be host:port, where port is an integer.")
                    else:
                        raise Exception("database_hostport specified is incorrect.  The format show be host:port, where port is an integer.")
            else:
                raise Exception("database_hostport specified must either be an emptry string or the host or host:port to connect to.")
        else:
            raise Exception("Must specify either 'database_socket' for 'database_hostport' for connection type.")
        
        
        if "database_username" in self._config and "database_password" in self._config:
            self._database_user = self._config["database_username"]
            self._database_password = self._config["database_password"]
        else:
            raise Exception("database_username and database_password must be specified in the configuration.")
            
        self._sample_interval_secs = 30 # how often to check the database status    
            
        if self._database_connect_type == "socket":
            self._db = MysqlDB (type = self._database_connect_type,
                                sockfile = self._database_socket,
                                host = None,
                                port = None,
                                username = self._database_user,
                                password = self._database_password,
                                logger = self._logger)
        else:
            self._db = MysqlDB (type = self._database_connect_type,
                                sockfile = None,
                                host = self._database_host,
                                port = self._database_port,
                                username = self._database_user,
                                password = self._database_password,
                                logger = self._logger)

    def gather_sample(self):
        """Invoked once per sample interval to gather a statistic.
        """
        def get_value_as_str(value):
            if type(value) is int:
                return "%d" % value
            elif type(value) is float:
                return "%f" % value
            elif type(value) is str:
                return "%r" % value
            else:
                return "%r" % value
            
        def print_status_line(key, value, extra_fields):
            """ Emit a status line.
            """
            self._logger.emit_value("mysql.%s" % key, value, extra_fields = extra_fields)
                
        def print_status(status):
            """print a status object, assumed to be a dictionary of key/values (and possibly extra fields).
            """
            if status is not None:
                for entry in status:
                    field = entry["field"]
                    value = entry["value"]
                    if "extra_fields" in entry:
                        extra_fields = entry["extra_fields"]
                    else:
                        extra_fields = None
                    print_status_line(field, value, extra_fields)
                    
        globalVars = self._db.gather_global_variables()
        globalStatus = self._db.gather_global_status()
        innodbStatus = self._db.gather_innodb_status(globalStatus)
        clusterStatus = self._db.gather_cluster_status()
        processInfo = self._db.gather_process_information()
        if globalVars is not None:
            print_status(globalVars)
        if globalStatus is not None:
            print_status(globalStatus)
        if innodbStatus is not None:
            print_status(innodbStatus)
        if clusterStatus is not None:
            print_status(clusterStatus)
        if processInfo is not None:
            print_status(processInfo)
            
        # calculate some derived stats
        if globalVars and globalStatus is not None:
            globalStatusMap = {}
            for f in globalStatus:
                globalStatusMap[f['field']] = f['value']
            globalVarsMap = {}
            for f in globalVars:
                globalVarsMap[f['field']] = f['value']
            calculatedStats = self._db.gather_derived_stats(globalVarsMap, globalStatusMap)
            if calculatedStats:
                print_status(calculatedStats)