#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2009, Giampaolo Rodola'. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""psutil is a module providing convenience functions for managing
processes and gather system information in a portable way by using
Python.
"""

from __future__ import division

__version__ = "1.3.0"
version_info = tuple([int(num) for num in __version__.split('.')])

__all__ = [
    # exceptions
    "Error", "NoSuchProcess", "AccessDenied", "TimeoutExpired",
    # constants
    "version_info", "__version__",
    "STATUS_RUNNING", "STATUS_IDLE", "STATUS_SLEEPING", "STATUS_DISK_SLEEP",
    "STATUS_STOPPED", "STATUS_TRACING_STOP", "STATUS_ZOMBIE", "STATUS_DEAD",
    "STATUS_WAKING", "STATUS_LOCKED",
    "CONN_ESTABLISHED", "CONN_SYN_SENT", "CONN_SYN_RECV", "CONN_FIN_WAIT1",
    "CONN_FIN_WAIT2", "CONN_TIME_WAIT", "CONN_CLOSE", "CONN_CLOSE_WAIT",
    "CONN_LAST_ACK", "CONN_LISTEN", "CONN_CLOSING", "CONN_NONE",
    # classes
    "Process", "Popen",
    # functions
    "pid_exists", "get_pids", "process_iter", "wait_procs",         # proc
    "virtual_memory", "swap_memory",                                # memory
    "cpu_times", "cpu_percent", "cpu_times_percent",                # cpu
    "net_io_counters",                                              # network
    "disk_io_counters", "disk_partitions", "disk_usage",            # disk
    "get_users", "get_boot_time",                                   # others
]

import sys
import os
import time
import signal
import warnings
import errno
import subprocess
try:
    import pwd
except ImportError:
    pwd = None

from psutil._error import Error, NoSuchProcess, AccessDenied, TimeoutExpired
from psutil._common import cached_property
from psutil._compat import property, callable, defaultdict, namedtuple
from psutil._compat import (wraps as _wraps,
                            PY3 as _PY3)
from psutil._common import (deprecated as _deprecated,
                            nt_disk_iostat as _nt_disk_iostat,
                            nt_net_iostat as _nt_net_iostat,
                            nt_sysmeminfo as _nt_sysmeminfo,
                            memoize as _memoize)

from psutil._common import (STATUS_RUNNING,
                            STATUS_IDLE,
                            STATUS_SLEEPING,
                            STATUS_DISK_SLEEP,
                            STATUS_STOPPED,
                            STATUS_TRACING_STOP,
                            STATUS_ZOMBIE,
                            STATUS_DEAD,
                            STATUS_WAKING,
                            STATUS_LOCKED)

from psutil._common import (CONN_ESTABLISHED,
                            CONN_SYN_SENT,
                            CONN_SYN_RECV,
                            CONN_FIN_WAIT1,
                            CONN_FIN_WAIT2,
                            CONN_TIME_WAIT,
                            CONN_CLOSE,
                            CONN_CLOSE_WAIT,
                            CONN_LAST_ACK,
                            CONN_LISTEN,
                            CONN_CLOSING,
                            CONN_NONE)

if sys.platform.startswith("linux"):
    import psutil._pslinux as _psplatform
    from psutil._pslinux import (phymem_buffers,
                                 cached_phymem)

    from psutil._pslinux import (IOPRIO_CLASS_NONE,
                                 IOPRIO_CLASS_RT,
                                 IOPRIO_CLASS_BE,
                                 IOPRIO_CLASS_IDLE)
    # Linux >= 2.6.36
    if _psplatform.HAS_PRLIMIT:
        from psutil._pslinux import (RLIM_INFINITY,
                                     RLIMIT_AS,
                                     RLIMIT_CORE,
                                     RLIMIT_CPU,
                                     RLIMIT_DATA,
                                     RLIMIT_FSIZE,
                                     RLIMIT_LOCKS,
                                     RLIMIT_MEMLOCK,
                                     RLIMIT_NOFILE,
                                     RLIMIT_NPROC,
                                     RLIMIT_RSS,
                                     RLIMIT_STACK)
        # Kinda ugly but considerably faster than using hasattr() and
        # setattr() against the module object (we are at import time
        # and we demand speed).
        try:
            RLIMIT_MSGQUEUE = _psplatform.RLIMIT_MSGQUEUE
        except AttributeError:
            pass
        try:
            RLIMIT_NICE = _psplatform.RLIMIT_NICE
        except AttributeError:
            pass
        try:
            RLIMIT_RTPRIO = _psplatform.RLIMIT_RTPRIO
        except AttributeError:
            pass
        try:
            RLIMIT_RTTIME = _psplatform.RLIMIT_RTTIME
        except AttributeError:
            pass
        try:
            RLIMIT_SIGPENDING = _psplatform.RLIMIT_SIGPENDING
        except AttributeError:
            pass

elif sys.platform.startswith("win32"):
    import psutil._pswindows as _psplatform
    from psutil._pswindows import (ABOVE_NORMAL_PRIORITY_CLASS,
                                   BELOW_NORMAL_PRIORITY_CLASS,
                                   HIGH_PRIORITY_CLASS,
                                   IDLE_PRIORITY_CLASS,
                                   NORMAL_PRIORITY_CLASS,
                                   REALTIME_PRIORITY_CLASS,
                                   CONN_DELETE_TCB)

elif sys.platform.startswith("darwin"):
    import psutil._psosx as _psplatform

elif sys.platform.startswith("freebsd"):
    import psutil._psbsd as _psplatform

elif sys.platform.startswith("sunos"):
    import psutil._pssunos as _psplatform
    from psutil._pssunos import (CONN_IDLE,
                                 CONN_BOUND)

else:
    raise NotImplementedError('platform %s is not supported' % sys.platform)

__all__.extend(_psplatform.__extra__all__)


_TOTAL_PHYMEM = None


def _assert_pid_not_reused(fun):
    """Decorator which raises NoSuchProcess in case a process is no
    longer running or its PID has been reused.
    """
    @_wraps(fun)
    def wrapper(self, *args, **kwargs):
        if not self.is_running():
            raise NoSuchProcess(self.pid, self._proc._process_name)
        return fun(self, *args, **kwargs)
    return wrapper


class Process(object):
    """Represents an OS process."""

    def __init__(self, pid=None):
        """Create a new Process object for the given pid.
        If pid is omitted current process pid is assumed.
        Raises NoSuchProcess if pid does not exist.

        Note that most of the methods of this class do not make sure
        the PID of the process being queried has been reused over time.
        That means you might end up retrieving an information referring
        to another process in case the original one this instance
        refers to is gone in the meantime.

        The only exceptions for which process identity is pre-emptively
        checked are:
         - parent
         - get_children()
         - set_nice()
         - set_rlimit()
         - suspend()
         - resume()
         - send_signal()
         - terminate()
         - kill()

        To prevent this problem for all other methods you can:
          - use is_running() before querying the process
          - if you're continuously iterating over a set of Process
            instances use process_iter() which pre-emptively checks
            process identity for every yielded instance
        """
        if pid is None:
            pid = os.getpid()
        else:
            if not _PY3:
                if not isinstance(pid, (int, long)):
                    raise TypeError('pid must be an integer (got %r)' % pid)
            if pid < 0:
                raise ValueError('pid must be a positive integer (got %s)'
                                 % pid)
        self._pid = pid
        self._gone = False
        # used for caching on Windows only (on POSIX ppid may change)
        self._ppid = None
        # platform-specific modules define an _psplatform.Process
        # implementation class
        self._proc = _psplatform.Process(pid)
        self._last_sys_cpu_times = None
        self._last_proc_cpu_times = None
        # cache creation time for later use in is_running() method
        try:
            self.create_time
        except AccessDenied:
            # we should never get here as AFAIK we're able to get
            # process creation time on all platforms even as a
            # limited user
            pass
        except NoSuchProcess:
            msg = 'no process found with pid %s' % pid
            raise NoSuchProcess(pid, None, msg)

    def __str__(self):
        try:
            pid = self.pid
            name = repr(self.name)
        except NoSuchProcess:
            details = "(pid=%s (terminated))" % self.pid
        except AccessDenied:
            details = "(pid=%s)" % (self.pid)
        else:
            details = "(pid=%s, name=%s)" % (pid, name)
        return "%s.%s%s" % (self.__class__.__module__,
                            self.__class__.__name__, details)

    def __repr__(self):
        return "<%s at %s>" % (self.__str__(), id(self))

    def __eq__(self, other):
        # Test for equality with another Process object based
        # on PID and creation time.
        # This pair is supposed to indentify a Process instance
        # univocally over time (the PID alone is not enough as
        # it might refer to a process whose PID has been reused).
        # Here we get the cached version of create_time which was
        # determined in __init__.
        # If not present it means AD was raised therefore the
        # comparison will be based on PID only.
        if not isinstance(other, Process):
            return False
        p1 = (self.pid, self.__dict__.get('create_time', None))
        p2 = (other.pid, other.__dict__.get('create_time', None))
        return p1 == p2

    def __hash__(self):
        return self.pid

    # --- utility methods

    def as_dict(self, attrs=[], ad_value=None):
        """Utility method returning process information as a hashable
        dictionary.

        If 'attrs' is specified it must be a list of strings reflecting
        available Process class's attribute names (e.g. ['get_cpu_times',
        'name']) else all public (read only) attributes are assumed.

        'ad_value' is the value which gets assigned to a dict key in case
        AccessDenied exception is raised when retrieving that particular
        process information.
        """
        excluded_names = set(
            ['send_signal', 'suspend', 'resume', 'terminate', 'kill', 'wait',
             'is_running', 'as_dict', 'parent', 'get_children', 'nice',
             'get_rlimit', 'getcwd'])
        retdict = dict()
        for name in set(attrs or dir(self)):
            if name.startswith('_'):
                continue
            if name.startswith('set_'):
                continue
            if name in excluded_names:
                continue
            try:
                attr = getattr(self, name)
                if callable(attr):
                    if name == 'get_cpu_percent':
                        ret = attr(interval=0)
                    else:
                        ret = attr()
                else:
                    ret = attr
            except AccessDenied:
                ret = ad_value
            except NotImplementedError:
                # in case of not implemented functionality (may happen
                # on old or exotic systems) we want to crash only if
                # the user explicitly asked for that particular attr
                if attrs:
                    raise
                continue
            if name.startswith('get'):
                if name[3] == '_':
                    name = name[4:]
                elif name == 'getcwd':
                    name = 'cwd'
            retdict[name] = ret
        return retdict

    @property
    @_assert_pid_not_reused
    def parent(self):
        """Return the parent process as a Process object pre-emptively
        checking whether PID has been reused.
        If no parent is known return None.
        """
        ppid = self.ppid
        if ppid is not None:
            try:
                parent = Process(ppid)
                if parent.create_time <= self.create_time:
                    return parent
                # ...else ppid has been reused by another process
            except NoSuchProcess:
                pass

    # --- actual API

    @property
    def pid(self):
        """The process pid."""
        return self._pid

    @property
    def ppid(self):
        """The process parent pid."""
        # On POSIX we don't want to cache the ppid as it may unexpectedly
        # change to 1 (init) in case this process turns into a zombie:
        # https://code.google.com/p/psutil/issues/detail?id=321
        # http://stackoverflow.com/questions/356722/

        # XXX should we check creation time here rather than in
        # Process.parent?
        if os.name == 'posix':
            return self._proc.get_ppid()
        else:
            if self._ppid is None:
                self._ppid = self._proc.get_ppid()
            return self._ppid

    @cached_property
    def name(self):
        """The process name."""
        name = self._proc.get_name()
        if os.name == 'posix' and len(name) >= 15:
            # On UNIX the name gets truncated to the first 15 characters.
            # If it matches the first part of the cmdline we return that
            # one instead because it's usually more explicative.
            # Examples are "gnome-keyring-d" vs. "gnome-keyring-daemon".
            try:
                cmdline = self.cmdline
            except AccessDenied:
                pass
            else:
                if cmdline:
                    extended_name = os.path.basename(cmdline[0])
                    if extended_name.startswith(name):
                        name = extended_name
        # XXX - perhaps needs refactoring
        self._proc._process_name = name
        return name

    @cached_property
    def exe(self):
        """The process executable path. May also be an empty string."""
        def guess_it(fallback):
            # try to guess exe from cmdline[0] in absence of a native
            # exe representation
            cmdline = self.cmdline
            if cmdline and hasattr(os, 'access') and hasattr(os, 'X_OK'):
                exe = cmdline[0]  # the possible exe
                # Attempt to guess only in case of an absolute path.
                # It is not safe otherwise as the process might have
                # changed cwd.
                if (os.path.isabs(exe)
                        and os.path.isfile(exe)
                        and os.access(exe, os.X_OK)):
                    return exe
            if isinstance(fallback, AccessDenied):
                raise fallback
            return fallback

        try:
            exe = self._proc.get_exe()
        except AccessDenied:
            err = sys.exc_info()[1]
            return guess_it(fallback=err)
        else:
            if not exe:
                # underlying implementation can legitimately return an
                # empty string; if that's the case we don't want to
                # raise AD while guessing from the cmdline
                try:
                    exe = guess_it(fallback=exe)
                except AccessDenied:
                    pass
            return exe

    @property
    def cmdline(self):
        """The command line process has been called with."""
        return self._proc.get_cmdline()

    @property
    def status(self):
        """The process current status as a STATUS_* constant."""
        return self._proc.get_status()

    if os.name == 'posix':

        @property
        def uids(self):
            """Return a named tuple denoting the process real,
            effective, and saved user ids.
            """
            return self._proc.get_uids()

        @property
        def gids(self):
            """Return a named tuple denoting the process real,
            effective, and saved group ids.
            """
            return self._proc.get_gids()

        @property
        def terminal(self):
            """The terminal associated with this process, if any,
            else None.
            """
            return self._proc.get_terminal()

    @property
    def username(self):
        """The name of the user that owns the process.
        On UNIX this is calculated by using *real* process uid.
        """
        if os.name == 'posix':
            if pwd is None:
                # might happen if python was installed from sources
                raise ImportError(
                    "requires pwd module shipped with standard python")
            return pwd.getpwuid(self.uids.real).pw_name
        else:
            return self._proc.get_username()

    @cached_property
    def create_time(self):
        """The process creation time as a floating point number
        expressed in seconds since the epoch, in UTC.
        """
        return self._proc.get_create_time()

    def get_cwd(self):
        """Return process current working directory."""
        return self._proc.get_cwd()

    # Linux, BSD and Windows only
    if hasattr(_psplatform.Process, "get_io_counters"):

        def get_io_counters(self):
            """Return process I/O statistics as a namedtuple including
            the number of read/write calls performed and the amount of
            bytes read and written by the process.
            """
            return self._proc.get_io_counters()

    def get_nice(self):
        """Get process niceness (priority)."""
        return self._proc.get_nice()

    @_assert_pid_not_reused
    def set_nice(self, value):
        """Set process niceness (priority) pre-emptively checking
        whether PID has been reused."""
        return self._proc.set_proc_nice(value)

    # Linux and Windows >= Vista only
    if hasattr(_psplatform.Process, "get_ionice"):

        def get_ionice(self):
            """Return process I/O niceness (priority).

            On Linux this is a (ioclass, value) namedtuple.
            On Windows it's an integer which can be equal to 2 (normal),
            1 (low) or 0 (very low).

            Available on Linux and Windows > Vista only.
            """
            return self._proc.get_ionice()

        def set_ionice(self, ioclass, value=None):
            """Set process I/O niceness (priority).

            On Linux 'ioclass' is one of the IOPRIO_CLASS_* constants.
            'value' is a number which goes from 0 to 7. The higher the
            value, the lower the I/O priority of the process.

            On Windows only 'ioclass' is used and it can be set to 2
            (normal), 1 (low) or 0 (very low).

            Available on Linux and Windows > Vista only.
            """
            return self._proc.set_proc_ionice(ioclass, value)

    # Linux only
    if hasattr(_psplatform.Process, "process_rlimit"):

        def get_rlimit(self, resource):
            """Get process resource limits as a (soft, hard) tuple.

            'resource' is one of the RLIMIT_* constants.

            See "man prlimit" for further info.
            """
            # if pid is 0 prlimit() applies to the calling process and
            # we don't want that
            if self.pid == 0:
                raise ValueError("can't use this method for PID 0 process")
            return self._proc.process_rlimit(resource)

        @_assert_pid_not_reused
        def set_rlimit(self, resource, limits):
            """Set process resource limits pre-emptively checking
            whether PID has been reused.

            'resource' is one of the RLIMIT_* constants.
            'limits' is supposed to be a (soft, hard) tuple.

            See "man prlimit" for further info.
            """
            # if pid is 0 prlimit() applies to the calling process and
            # we don't want that
            if self.pid == 0:
                raise ValueError("can't use this method for PID 0 process")
            self._proc.process_rlimit(resource, limits)

    # Windows and Linux only
    if hasattr(_psplatform.Process, "get_cpu_affinity"):

        def get_cpu_affinity(self):
            """Get process current CPU affinity."""
            return self._proc.get_cpu_affinity()

        def set_cpu_affinity(self, cpus):
            """Set process current CPU affinity.
            'cpus' is a list of CPUs for which you want to set the
            affinity (e.g. [0, 1]).
            """
            return self._proc.set_proc_cpu_affinity(cpus)

    if os.name == 'nt':

        def get_num_handles(self):
            """Return the number of handles opened by this process
            (Windows only).
            """
            return self._proc.get_num_handles()

    if os.name == 'posix':

        def get_num_fds(self):
            """Return the number of file descriptors opened by this
            process (POSIX only).
            """
            return self._proc.get_num_fds()

    def get_num_ctx_switches(self):
        """Return the number voluntary and involuntary context switches
        performed by this process.
        """
        return self._proc.get_num_ctx_switches()

    def get_num_threads(self):
        """Return the number of threads used by this process."""
        return self._proc.get_num_threads()

    def get_threads(self):
        """Return threads opened by process as a list of namedtuples
        including thread id and thread CPU times (user/system).
        """
        return self._proc.get_threads()

    @_assert_pid_not_reused
    def get_children(self, recursive=False):
        """Return the children of this process as a list of Process
        objects pre-emptively checking whether PID has been reused.
        If recursive is True return all the parent descendants.

        Example (A == this process):

         A ─┐
            │
            ├─ B (child) ─┐
            │             └─ X (grandchild) ─┐
            │                                └─ Y (great grandchild)
            ├─ C (child)
            └─ D (child)

        >>> import psutil
        >>> p = psutil.Process()
        >>> p.get_children()
        B, C, D
        >>> p.get_children(recursive=True)
        B, X, Y, C, D

        Note that in the example above if process X disappears
        process Y won't be returned either as the reference to
        process A is lost.
        """
        if hasattr(_psplatform, 'get_ppid_map'):
            # Windows only: obtain a {pid:ppid, ...} dict for all running
            # processes in one shot (faster).
            ppid_map = _psplatform.get_ppid_map()
        else:
            ppid_map = None

        ret = []
        if not recursive:
            if ppid_map is None:
                # 'slow' version, common to all platforms except Windows
                for p in process_iter():
                    try:
                        if p.ppid == self.pid:
                            # if child happens to be older than its parent
                            # (self) it means child's PID has been reused
                            if self.create_time <= p.create_time:
                                ret.append(p)
                    except NoSuchProcess:
                        pass
            else:
                # Windows only (faster)
                for pid, ppid in ppid_map.items():
                    if ppid == self.pid:
                        try:
                            child = Process(pid)
                            # if child happens to be older than its parent
                            # (self) it means child's PID has been reused
                            if self.create_time <= child.create_time:
                                ret.append(child)
                        except NoSuchProcess:
                            pass
        else:
            # construct a dict where 'values' are all the processes
            # having 'key' as their parent
            table = defaultdict(list)
            if ppid_map is None:
                for p in process_iter():
                    try:
                        table[p.ppid].append(p)
                    except NoSuchProcess:
                        pass
            else:
                for pid, ppid in ppid_map.items():
                    try:
                        p = Process(pid)
                        table[ppid].append(p)
                    except NoSuchProcess:
                        pass
            # At this point we have a mapping table where table[self.pid]
            # are the current process' children.
            # Below, we look for all descendants recursively, similarly
            # to a recursive function call.
            checkpids = [self.pid]
            for pid in checkpids:
                for child in table[pid]:
                    try:
                        # if child happens to be older than its parent
                        # (self) it means child's PID has been reused
                        intime = self.create_time <= child.create_time
                    except NoSuchProcess:
                        pass
                    else:
                        if intime:
                            ret.append(child)
                            if child.pid not in checkpids:
                                checkpids.append(child.pid)
        return ret

    def get_cpu_percent(self, interval=0.1):
        """Return a float representing the current process CPU
        utilization as a percentage.

        When interval is > 0.0 compares process times to system CPU
        times elapsed before and after the interval (blocking).

        When interval is 0.0 or None compares process times to system
        CPU times elapsed since last call, returning immediately
        (non-blocking).
        In this case is recommended for accuracy that this function
        be called with at least 0.1 seconds between calls.

        Examples:

          >>> import psutil
          >>> p = psutil.Process(os.getpid())
          >>> # blocking
          >>> p.get_cpu_percent(interval=1)
          2.0
          >>> # non-blocking (percentage since last call)
          >>> p.get_cpu_percent(interval=0)
          2.9
          >>>
        """
        blocking = interval is not None and interval > 0.0
        if blocking:
            st1 = sum(cpu_times())
            pt1 = self._proc.get_cpu_times()
            time.sleep(interval)
            st2 = sum(cpu_times())
            pt2 = self._proc.get_cpu_times()
        else:
            st1 = self._last_sys_cpu_times
            pt1 = self._last_proc_cpu_times
            st2 = sum(cpu_times())
            pt2 = self._proc.get_cpu_times()
            if st1 is None or pt1 is None:
                self._last_sys_cpu_times = st2
                self._last_proc_cpu_times = pt2
                return 0.0

        delta_proc = (pt2.user - pt1.user) + (pt2.system - pt1.system)
        delta_time = st2 - st1
        # reset values for next call in case of interval == None
        self._last_sys_cpu_times = st2
        self._last_proc_cpu_times = pt2

        try:
            # the utilization split between all CPUs
            overall_percent = (delta_proc / delta_time) * 100
        except ZeroDivisionError:
            # interval was too low
            return 0.0
        # the utilization of a single CPU (note: cpu_count() value is cached)
        single_cpu_percent = overall_percent * cpu_count()
        # On POSIX a percentage > 100 is legitimate:
        # http://stackoverflow.com/questions/1032357/
        #   comprehending-top-cpu-usage
        # On windows we use this ugly hack in order to avoid float
        # precision issues.
        if os.name != 'posix':
            if single_cpu_percent > 100.0:
                return 100.0
        return round(single_cpu_percent, 1)

    def get_cpu_times(self):
        """Return a tuple whose values are process CPU user and system
        times. The same as os.times() but per-process.
        """
        return self._proc.get_cpu_times()

    def get_memory_info(self):
        """Return a tuple representing RSS (Resident Set Size) and VMS
        (Virtual Memory Size) in bytes.

        On UNIX RSS and VMS are the same values shown by ps.

        On Windows RSS and VMS refer to "Mem Usage" and "VM Size" columns
        of taskmgr.exe.
        """
        return self._proc.get_memory_info()

    def get_ext_memory_info(self):
        """Return a namedtuple with variable fields depending on the
        platform representing extended memory information about
        the process. All numbers are expressed in bytes.
        """
        return self._proc.get_ext_memory_info()

    def get_memory_percent(self):
        """Compare physical system memory to process resident memory
        (RSS) and calculate process memory utilization as a percentage.
        """
        rss = self._proc.get_memory_info()[0]
        # use cached value if available
        total_phymem = _TOTAL_PHYMEM or virtual_memory().total
        try:
            return (rss / float(total_phymem)) * 100
        except ZeroDivisionError:
            return 0.0

    def get_memory_maps(self, grouped=True):
        """Return process's mapped memory regions as a list of nameduples
        whose fields are variable depending on the platform.

        If 'grouped' is True the mapped regions with the same 'path'
        are grouped together and the different memory fields are summed.

        If 'grouped' is False every mapped region is shown as a single
        entity and the namedtuple will also include the mapped region's
        address space ('addr') and permission set ('perms').
        """
        it = self._proc.get_memory_maps()
        if grouped:
            d = {}
            for tupl in it:
                path = tupl[2]
                nums = tupl[3:]
                try:
                    d[path] = map(lambda x, y: x + y, d[path], nums)
                except KeyError:
                    d[path] = nums
            nt = self._proc.nt_mmap_grouped
            return [nt(path, *d[path]) for path in d]
        else:
            nt = self._proc.nt_mmap_ext
            return [nt(*x) for x in it]

    def get_open_files(self):
        """Return files opened by process as a list of namedtuples
        including absolute file name and file descriptor number.
        """
        return self._proc.get_open_files()

    def get_connections(self, kind='inet'):
        """Return connections opened by process as a list of namedtuples.
        The kind parameter filters for connections that fit the following
        criteria:

        Kind Value      Connections using
        inet            IPv4 and IPv6
        inet4           IPv4
        inet6           IPv6
        tcp             TCP
        tcp4            TCP over IPv4
        tcp6            TCP over IPv6
        udp             UDP
        udp4            UDP over IPv4
        udp6            UDP over IPv6
        unix            UNIX socket (both UDP and TCP protocols)
        all             the sum of all the possible families and protocols
        """
        return self._proc.get_connections(kind)

    def is_running(self):
        """Return whether this process is running.
        It also checks if PID has been reused by another process in
        which case returns False.
        """
        if self._gone:
            return False
        try:
            # Checking if pid is alive is not enough as the pid might
            # have been reused by another process: we also want to
            # check process identity.
            # Process identity / uniqueness over time is greanted by
            # (pid + creation time) and that is verified in __eq__.
            return self == Process(self.pid)
        except NoSuchProcess:
            self._gone = True
            return False

    @_assert_pid_not_reused
    def send_signal(self, sig):
        """Send a signal to process pre-emptively checking whether
        PID has been reused (see signal module constants) .
        On Windows only SIGTERM is valid and is treated as an alias
        for kill().
        """
        if os.name == 'posix':
            try:
                os.kill(self.pid, sig)
            except OSError:
                err = sys.exc_info()[1]
                name = self._proc._process_name
                if err.errno == errno.ESRCH:
                    self._gone = True
                    raise NoSuchProcess(self.pid, name)
                if err.errno == errno.EPERM:
                    raise AccessDenied(self.pid, name)
                raise
        else:
            if sig == signal.SIGTERM:
                self._proc.kill_process()
            else:
                raise ValueError("only SIGTERM is supported on Windows")

    @_assert_pid_not_reused
    def suspend(self):
        """Suspend process execution with SIGSTOP pre-emptively checking
        whether PID has been reused.
        On Windows it suspends all process threads.
        """
        if hasattr(self._proc, "suspend_process"):
            # windows
            self._proc.suspend_process()
        else:
            # posix
            self.send_signal(signal.SIGSTOP)

    @_assert_pid_not_reused
    def resume(self):
        """Resume process execution with SIGCONT pre-emptively checking
        whether PID has been reused.
        On Windows it resumes all process threads.
        """
        if hasattr(self._proc, "resume_process"):
            # windows
            self._proc.resume_process()
        else:
            # posix
            self.send_signal(signal.SIGCONT)

    def terminate(self):
        """Terminate the process with SIGTERM pre-emptively checking
        whether PID has been reused.
        On Windows this is an alias for kill().
        """
        self.send_signal(signal.SIGTERM)

    @_assert_pid_not_reused
    def kill(self):
        """Kill the current process with SIGKILL pre-emptively checking
        whether PID has been reused."""
        if os.name == 'posix':
            self.send_signal(signal.SIGKILL)
        else:
            self._proc.kill_process()

    def wait(self, timeout=None):
        """Wait for process to terminate and, if process is a children
        of the current one also return its exit code, else None.

        If the process is already terminated immediately return None
        instead of raising NoSuchProcess.

        If timeout (in seconds) is specified and process is still alive
        after the timeout expired raise TimeoutExpired.
        """
        if timeout is not None and not timeout >= 0:
            raise ValueError("timeout must be a positive integer")
        return self._proc.process_wait(timeout)

    # --- deprecated API

    @property
    def nice(self):
        """Get or set process niceness (priority).
        Deprecated, use get_nice() instead.
        """
        msg = "this property is deprecated; " \
              "use Process.get_nice() method instead"
        warnings.warn(msg, category=DeprecationWarning, stacklevel=2)
        return self.get_nice()

    @nice.setter
    def nice(self, value):
        # invoked on "p.nice = num"; change process niceness
        # deprecated in favor of set_nice()
        msg = "this property is deprecated; " \
              "use Process.set_nice() method instead"
        warnings.warn(msg, category=DeprecationWarning, stacklevel=2)
        return self.set_nice(value)

    def getcwd(self):
        """Return process current working directory.
        Deprecated, use get_cwd() instead.
        """
        msg = "this method is deprecated; use Process.get_cwd() instead"
        warnings.warn(msg, category=DeprecationWarning, stacklevel=2)
        return self.get_cwd()


class Popen(Process):
    """A more convenient interface to stdlib subprocess module.
    It starts a sub process and deals with it exactly as when using
    subprocess.Popen class but in addition also provides all the
    property and methods of psutil.Process class in a single interface:

      >>> import psutil
      >>> from subprocess import PIPE
      >>> p = psutil.Popen(["python", "-c", "print 'hi'"], stdout=PIPE)
      >>> p.name
      'python'
      >>> p.uids
      user(real=1000, effective=1000, saved=1000)
      >>> p.username
      'giampaolo'
      >>> p.communicate()
      ('hi\n', None)
      >>> p.terminate()
      >>> p.wait(timeout=2)
      0
      >>>

    For method names common to both classes such as kill(), terminate()
    and wait(), psutil.Process implementation takes precedence.

    For a complete documentation refers to:
    http://docs.python.org/library/subprocess.html
    """

    def __init__(self, *args, **kwargs):
        # Here we avoid to call the original Process constructor
        # because if the process spawned by subprocess.Popen
        # terminates too quickly we'll get NoSuchProcess, see:
        # https://code.google.com/p/psutil/issues/detail?id=193
        self.__subproc = subprocess.Popen(*args, **kwargs)
        self._pid = self.__subproc.pid
        self._gone = False
        self._ppid = None
        self._proc = _psplatform.Process(self._pid)
        self._last_sys_cpu_times = None
        self._last_proc_cpu_times = None
        try:
            self.create_time
        except AccessDenied:
            pass
        except NoSuchProcess:
            raise NoSuchProcess(self._pid, None,
                                "no process found with pid %s" % self._pid)

    def __dir__(self):
        return list(set(dir(Popen) + dir(subprocess.Popen)))

    def __getattribute__(self, name):
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            try:
                return object.__getattribute__(self.__subproc, name)
            except AttributeError:
                raise AttributeError("%s instance has no attribute '%s'"
                                     % (self.__class__.__name__, name))


# =====================================================================
# --- system processes related functions
# =====================================================================

def get_pids():
    """Return a list of current running PIDs."""
    return _psplatform.get_pids()


def pid_exists(pid):
    """Return True if given PID exists in the current process list.
    This is faster than doing "pid in psutil.get_pids()" and
    should be preferred.
    """
    return _psplatform.pid_exists(pid)


_pmap = {}

def process_iter():
    """Return a generator yielding a Process class instance for all
    running processes on the local machine.

    Every new Process instance is only created once and then cached
    into an internal table which is updated every time this is used.

    Cached Process instances are checked for identity so that you're
    safe in case a PID has been reused by another process, in which
    case the cached instance is updated.

    The sorting order in which processes are yielded is based on
    their PIDs.
    """
    def add(pid):
        proc = Process(pid)
        _pmap[proc.pid] = proc
        return proc

    def remove(pid):
        _pmap.pop(pid, None)

    a = set(get_pids())
    b = set(_pmap.keys())
    new_pids = a - b
    gone_pids = b - a

    for pid in gone_pids:
        remove(pid)
    for pid, proc in sorted(list(_pmap.items()) +
                            list(dict.fromkeys(new_pids).items())):
        try:
            if proc is None:  # new process
                yield add(pid)
            else:
                # use is_running() to check whether PID has been reused by
                # another process in which case yield a new Process instance
                if proc.is_running():
                    yield proc
                else:
                    yield add(pid)
        except NoSuchProcess:
            remove(pid)
        except AccessDenied:
            # Process creation time can't be determined hence there's
            # no way to tell whether the pid of the cached process
            # has been reused. Just return the cached version.
            yield proc


def wait_procs(procs, timeout=None, callback=None):
    """Convenience function which waits for a list of processes to
    terminate.

    Return a (gone, alive) tuple indicating which processes
    are gone and which ones are still alive.

    The gone ones will have a new 'retcode' attribute indicating
    process exit status (may be None).

    'callback' is a callable function which gets called every
    time a process terminates (a Process instance is passed as
    callback argument).

    Function will return as soon as all processes terminate or when
    timeout occurs.

    Tipical use case is:

     - send SIGTERM to a list of processes
     - give them some time to terminate
     - send SIGKILL to those ones which are still alive

    Example:

    >>> def on_terminate(proc):
    ...     print("process {} terminated".format(proc))
    ...
    >>> for p in procs:
    ...    p.terminate()
    ...
    >>> gone, alive = wait_procs(procs, timeout=3, callback=on_terminate)
    >>> for p in alive:
    ...     p.kill()
    """
    def assert_gone(proc, timeout):
        try:
            retcode = proc.wait(timeout=timeout)
        except TimeoutExpired:
            pass
        else:
            if retcode is not None or not proc.is_running():
                proc.retcode = retcode
                gone.add(proc)
                if callback is not None:
                    callback(proc)

    if timeout is not None and not timeout >= 0:
        msg = "timeout must be a positive integer, got %s" % timeout
        raise ValueError(msg)
    timer = getattr(time, 'monotonic', time.time)
    gone = set()
    alive = set(procs)
    if callback is not None and not callable(callback):
        raise TypeError("callback %r is not a callable" % callable)
    if timeout is not None:
        deadline = timer() + timeout

    while alive:
        if timeout is not None and timeout <= 0:
            break
        for proc in alive:
            # Make sure that every complete iteration (all processes)
            # will last max 1 sec.
            # We do this because we don't want to wait too long on a
            # single process: in case it terminates too late other
            # processes may disappear in the meantime and their PID
            # reused.
            try:
                max_timeout = 1.0 / (len(alive) - len(gone))
            except ZeroDivisionError:
                max_timeout = 1.0  # one alive remaining
            if timeout is not None:
                timeout = min((deadline - timer()), max_timeout)
                if timeout <= 0:
                    break
                assert_gone(proc, timeout)
            else:
                assert_gone(proc, max_timeout)
        alive = alive - gone

    if alive:
        # Last attempt over processes survived so far.
        # timeout == 0 won't make this function wait any further.
        for proc in alive:
            assert_gone(proc, 0)
        alive = alive - gone

    return (list(gone), list(alive))


# =====================================================================
# --- CPU related functions
# =====================================================================

@_memoize
def cpu_count():
    """Return the number of logical CPUs in the system.
    Return None if undetermined.
    This is the same as os.cpu_count() from Python 3.4 and
    replaces the deprecated psutil.NUM_CPUS constant.
    """
    return _psplatform.get_num_cpus()


def cpu_times(percpu=False):
    """Return system-wide CPU times as a namedtuple object.
    Every CPU time represents the time CPU has spent in the given mode.
    The attributes availability varies depending on the platform.
    Here follows a list of all available attributes:
     - user
     - system
     - idle
     - nice (UNIX)
     - iowait (Linux)
     - irq (Linux, FreeBSD)
     - softirq (Linux)
     - steal (Linux >= 2.6.11)
     - guest (Linux >= 2.6.24)
     - guest_nice (Linux >= 3.2.0)

    When percpu is True return a list of nameduples for each CPU.
    First element of the list refers to first CPU, second element
    to second CPU and so on.
    The order of the list is consistent across calls.
    """
    if not percpu:
        return _psplatform.get_system_cpu_times()
    else:
        return _psplatform.get_system_per_cpu_times()


_last_cpu_times = cpu_times()
_last_per_cpu_times = cpu_times(percpu=True)

def cpu_percent(interval=0.1, percpu=False):
    """Return a float representing the current system-wide CPU
    utilization as a percentage.

    When interval is > 0.0 compares system CPU times elapsed before
    and after the interval (blocking).

    When interval is 0.0 or None compares system CPU times elapsed
    since last call or module import, returning immediately.
    In this case is recommended for accuracy that this function be
    called with at least 0.1 seconds between calls.

    When percpu is True returns a list of floats representing the
    utilization as a percentage for each CPU.
    First element of the list refers to first CPU, second element
    to second CPU and so on.
    The order of the list is consistent across calls.

    Examples:

      >>> # blocking, system-wide
      >>> psutil.cpu_percent(interval=1)
      2.0
      >>>
      >>> # blocking, per-cpu
      >>> psutil.cpu_percent(interval=1, percpu=True)
      [2.0, 1.0]
      >>>
      >>> # non-blocking (percentage since last call)
      >>> psutil.cpu_percent(interval=0)
      2.9
      >>>
    """
    global _last_cpu_times
    global _last_per_cpu_times
    blocking = interval is not None and interval > 0.0

    def calculate(t1, t2):
        t1_all = sum(t1)
        t1_busy = t1_all - t1.idle

        t2_all = sum(t2)
        t2_busy = t2_all - t2.idle

        # this usually indicates a float precision issue
        if t2_busy <= t1_busy:
            return 0.0

        busy_delta = t2_busy - t1_busy
        all_delta = t2_all - t1_all
        busy_perc = (busy_delta / all_delta) * 100
        return round(busy_perc, 1)

    # system-wide usage
    if not percpu:
        if blocking:
            t1 = cpu_times()
            time.sleep(interval)
        else:
            t1 = _last_cpu_times
        _last_cpu_times = cpu_times()
        return calculate(t1, _last_cpu_times)
    # per-cpu usage
    else:
        ret = []
        if blocking:
            tot1 = cpu_times(percpu=True)
            time.sleep(interval)
        else:
            tot1 = _last_per_cpu_times
        _last_per_cpu_times = cpu_times(percpu=True)
        for t1, t2 in zip(tot1, _last_per_cpu_times):
            ret.append(calculate(t1, t2))
        return ret


# Use separate global vars for cpu_times_percent() so that it's
# independent from cpu_percent() and they can both be used within
# the same program.
_last_cpu_times_2 = _last_cpu_times
_last_per_cpu_times_2 = _last_per_cpu_times
_ptime_cpu_perc_nt = None

def cpu_times_percent(interval=0.1, percpu=False):
    """Same as cpu_percent() but provides utilization percentages
    for each specific CPU time as is returned by cpu_times().
    For instance, on Linux we'll get:

      >>> cpu_times_percent()
      cpupercent(user=4.8, nice=0.0, system=4.8, idle=90.5, iowait=0.0,
                 irq=0.0, softirq=0.0, steal=0.0, guest=0.0, guest_nice=0.0)
      >>>

    interval and percpu arguments have the same meaning as in
    cpu_percent().
    """
    global _last_cpu_times_2
    global _last_per_cpu_times_2
    blocking = interval is not None and interval > 0.0
    WINDOWS = os.name == 'nt'

    def calculate(t1, t2):
        global _ptime_cpu_perc_nt
        nums = []
        all_delta = sum(t2) - sum(t1)
        for field in t1._fields:
            field_delta = getattr(t2, field) - getattr(t1, field)
            try:
                field_perc = (100 * field_delta) / all_delta
            except ZeroDivisionError:
                field_perc = 0.0
            field_perc = round(field_perc, 1)
            if WINDOWS:
                # XXX
                # Work around:
                # https://code.google.com/p/psutil/issues/detail?id=392
                # CPU times are always supposed to increase over time
                # or at least remain the same and that's because time
                # cannot go backwards.
                # Surprisingly sometimes this might not be the case on
                # Windows where 'system' CPU time can be smaller
                # compared to the previous call, resulting in corrupted
                # percentages (< 0 or > 100).
                # I really don't know what to do about that except
                # forcing the value to 0 or 100.
                if field_perc > 100.0:
                    field_perc = 100.0
                elif field_perc < 0.0:
                    field_perc = 0.0
            nums.append(field_perc)
        if _ptime_cpu_perc_nt is None:
            _ptime_cpu_perc_nt = namedtuple('cpupercent', ' '.join(t1._fields))
        return _ptime_cpu_perc_nt(*nums)

    # system-wide usage
    if not percpu:
        if blocking:
            t1 = cpu_times()
            time.sleep(interval)
        else:
            t1 = _last_cpu_times_2
        _last_cpu_times_2 = cpu_times()
        return calculate(t1, _last_cpu_times_2)
    # per-cpu usage
    else:
        ret = []
        if blocking:
            tot1 = cpu_times(percpu=True)
            time.sleep(interval)
        else:
            tot1 = _last_per_cpu_times_2
        _last_per_cpu_times_2 = cpu_times(percpu=True)
        for t1, t2 in zip(tot1, _last_per_cpu_times_2):
            ret.append(calculate(t1, t2))
        return ret


# =====================================================================
# --- system memory related functions
# =====================================================================

def virtual_memory():
    """Return statistics about system memory usage as a namedtuple
    including the following fields, expressed in bytes:

     - total:
       total physical memory available.

     - available:
       the actual amount of available memory that can be given
       instantly to processes that request more memory in bytes; this
       is calculated by summing different memory values depending on
       the platform (e.g. free + buffers + cached on Linux) and it is
       supposed to be used to monitor actual memory usage in a cross
       platform fashion.

     - percent:
       the percentage usage calculated as (total - available) / total * 100

     - used:
       memory used, calculated differently depending on the platform and
       designed for informational purposes only:
        OSX: active + inactive + wired
        BSD: active + wired + cached
        LINUX: total - free

     - free:
       memory not being used at all (zeroed) that is readily available;
       note that this doesn't reflect the actual memory available
       (use 'available' instead)

    Platform-specific fields:

     - active (UNIX):
       memory currently in use or very recently used, and so it is in RAM.

     - inactive (UNIX):
       memory that is marked as not used.

     - buffers (BSD, Linux):
       cache for things like file system metadata.

     - cached (BSD, OSX):
       cache for various things.

     - wired (OSX, BSD):
       memory that is marked to always stay in RAM. It is never moved to disk.

     - shared (BSD):
       memory that may be simultaneously accessed by multiple processes.

    The sum of 'used' and 'available' does not necessarily equal total.
    On Windows 'available' and 'free' are the same.
    """
    global _TOTAL_PHYMEM
    ret = _psplatform.virtual_memory()
    # cached for later use in Process.get_memory_percent()
    _TOTAL_PHYMEM = ret.total
    return ret


def swap_memory():
    """Return system swap memory statistics as a namedtuple including
    the following attributes:

     - total:   total swap memory in bytes
     - used:    used swap memory in bytes
     - free:    free swap memory in bytes
     - percent: the percentage usage
     - sin:     no. of bytes the system has swapped in from disk (cumulative)
     - sout:    no. of bytes the system has swapped out from disk (cumulative)

    'sin' and 'sout' on Windows are meaningless and always set to 0.
    """
    return _psplatform.swap_memory()


# =====================================================================
# --- disks/paritions related functions
# =====================================================================

def disk_usage(path):
    """Return disk usage statistics about the given path as a namedtuple
    including total, used and free space expressed in bytes plus the
    percentage usage.
    """
    return _psplatform.get_disk_usage(path)


def disk_partitions(all=False):
    """Return mounted partitions as a list of namedtuples including
    device, mount point, filesystem type and mount options (a raw
    string separated by commas which may vary depending on the platform).

    If "all" parameter is False return physical devices only and ignore
    all others.
    """
    return _psplatform.disk_partitions(all)


def disk_io_counters(perdisk=False):
    """Return system disk I/O statistics as a namedtuple including
    the following attributes:

     - read_count:  number of reads
     - write_count: number of writes
     - read_bytes:  number of bytes read
     - write_bytes: number of bytes written
     - read_time:   time spent reading from disk (in milliseconds)
     - write_time:  time spent writing to disk (in milliseconds)

    If perdisk is True return the same information for every
    physical disk installed on the system as a dictionary
    with partition names as the keys and the namedutuple
    described above as the values.

    On recent Windows versions 'diskperf -y' command may need to be
    executed first otherwise this function won't find any disk.
    """
    rawdict = _psplatform.disk_io_counters()
    if not rawdict:
        raise RuntimeError("couldn't find any physical disk")
    if perdisk:
        for disk, fields in rawdict.items():
            rawdict[disk] = _nt_disk_iostat(*fields)
        return rawdict
    else:
        return _nt_disk_iostat(*[sum(x) for x in zip(*rawdict.values())])


# =====================================================================
# --- network related functions
# =====================================================================

def net_io_counters(pernic=False):
    """Return network I/O statistics as a namedtuple including
    the following attributes:

     - bytes_sent:   number of bytes sent
     - bytes_recv:   number of bytes received
     - packets_sent: number of packets sent
     - packets_recv: number of packets received
     - errin:        total number of errors while receiving
     - errout:       total number of errors while sending
     - dropin:       total number of incoming packets which were dropped
     - dropout:      total number of outgoing packets which were dropped
                     (always 0 on OSX and BSD)

    If pernic is True return the same information for every
    network interface installed on the system as a dictionary
    with network interface names as the keys and the namedtuple
    described above as the values.
    """
    rawdict = _psplatform.net_io_counters()
    if not rawdict:
        raise RuntimeError("couldn't find any network interface")
    if pernic:
        for nic, fields in rawdict.items():
            rawdict[nic] = _nt_net_iostat(*fields)
        return rawdict
    else:
        return _nt_net_iostat(*[sum(x) for x in zip(*rawdict.values())])


# =====================================================================
# --- other system related functions
# =====================================================================

def get_boot_time():
    """Return the system boot time expressed in seconds since the epoch.
    This is also available as psutil.BOOT_TIME.
    """
    # Note: we are not caching this because it is subject to
    # system clock updates.
    return _psplatform.get_system_boot_time()


def get_users():
    """Return users currently connected on the system as a list of
    namedtuples including the following attributes.

     - user: the name of the user
     - terminal: the tty or pseudo-tty associated with the user, if any.
     - host: the host name associated with the entry, if any.
     - started: the creation time as a floating point number expressed in
       seconds since the epoch.
    """
    return _psplatform.get_system_users()


# =====================================================================
# --- deprecated functions
# =====================================================================

@_deprecated("psutil.get_pids()")
def get_pid_list():
    """Return a list of current running PIDs (deprecated)."""
    return get_pids()


@_deprecated()
def get_process_list():
    """Return a list of Process class instances for all running
    processes on the local machine (deprecated).
    """
    return list(process_iter())


@_deprecated()
def phymem_usage():
    """Return the amount of total, used and free physical memory
    on the system in bytes plus the percentage usage.
    Deprecated by psutil.virtual_memory().
    """
    mem = virtual_memory()
    return _nt_sysmeminfo(mem.total, mem.used, mem.free, mem.percent)


@_deprecated("psutil.swap_memory()")
def virtmem_usage():
    return swap_memory()


@_deprecated("psutil.phymem_usage().free")
def avail_phymem():
    return phymem_usage().free


@_deprecated("psutil.phymem_usage().used")
def used_phymem():
    return phymem_usage().used


@_deprecated("psutil.virtmem_usage().total")
def total_virtmem():
    return virtmem_usage().total


@_deprecated("psutil.virtmem_usage().used")
def used_virtmem():
    return virtmem_usage().used


@_deprecated("psutil.virtmem_usage().free")
def avail_virtmem():
    return virtmem_usage().free


@_deprecated("psutil.net_io_counters()")
def network_io_counters(pernic=False):
    return net_io_counters(pernic)


def _replace_module():
    """Dirty hack to replace the module object in order to access
    deprecated module constants, see:
    http://www.dr-josiah.com/2013/12/properties-on-python-modules.html
    """
    class ModuleWrapper(object):

        def __repr__(self):
            return repr(self._module)
        __str__ = __repr__

        @property
        def NUM_CPUS(self):
            msg = "NUM_CPUS constant is deprecated; use cpu_count() instead"
            warnings.warn(msg, category=DeprecationWarning, stacklevel=2)
            return cpu_count()

        @property
        def BOOT_TIME(self):
            msg = "BOOT_TIME constant is deprecated; " \
                  "use get_boot_time() instead"
            warnings.warn(msg, category=DeprecationWarning, stacklevel=2)
            return get_boot_time()

        @property
        def TOTAL_PHYMEM(self):
            msg = "TOTAL_PHYMEM constant is deprecated; " \
                  "use virtual_memory().total instead"
            warnings.warn(msg, category=DeprecationWarning, stacklevel=2)
            return virtual_memory().total

    mod = ModuleWrapper()
    mod.__dict__ = globals()
    mod._module = sys.modules[__name__]
    sys.modules[__name__] = mod


def test():
    """List info of all currently running processes emulating ps aux
    output.
    """
    import datetime
    from psutil._compat import print_

    today_day = datetime.date.today()
    templ = "%-10s %5s %4s %4s %7s %7s %-13s %5s %7s  %s"
    attrs = ['pid', 'get_cpu_percent', 'get_memory_percent', 'name',
             'get_cpu_times', 'create_time', 'get_memory_info']
    if os.name == 'posix':
        attrs.append('uids')
        attrs.append('terminal')
    print_(templ % ("USER", "PID", "%CPU", "%MEM", "VSZ", "RSS", "TTY",
                    "START", "TIME", "COMMAND"))
    for p in sorted(process_iter(), key=lambda p: p.pid):
        try:
            pinfo = p.as_dict(attrs, ad_value='')
        except NoSuchProcess:
            pass
        else:
            if pinfo['create_time']:
                ctime = datetime.datetime.fromtimestamp(pinfo['create_time'])
                if ctime.date() == today_day:
                    ctime = ctime.strftime("%H:%M")
                else:
                    ctime = ctime.strftime("%b%d")
            else:
                ctime = ''
            cputime = time.strftime("%M:%S",
                                    time.localtime(sum(pinfo['cpu_times'])))
            try:
                user = p.username
            except KeyError:
                if os.name == 'posix':
                    if pinfo['uids']:
                        user = str(pinfo['uids'].real)
                    else:
                        user = ''
                else:
                    raise
            except Error:
                user = ''
            if os.name == 'nt' and '\\' in user:
                user = user.split('\\')[1]
            vms = pinfo['memory_info'] and \
                int(pinfo['memory_info'].vms / 1024) or '?'
            rss = pinfo['memory_info'] and \
                int(pinfo['memory_info'].rss / 1024) or '?'
            memp = pinfo['memory_percent'] and \
                round(pinfo['memory_percent'], 1) or '?'
            print_(templ % (user[:10],
                            pinfo['pid'],
                            pinfo['cpu_percent'],
                            memp,
                            vms,
                            rss,
                            pinfo.get('terminal', '') or '?',
                            ctime,
                            cputime,
                            pinfo['name'].strip() or '?'))


_replace_module()
del property, cached_property, division, _replace_module
if sys.version_info < (3, 0):
    del num


if __name__ == "__main__":
    test()
