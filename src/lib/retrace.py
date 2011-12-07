import ConfigParser
import errno
import gettext
import os
import re
import random
import shutil
import sqlite3
import time
from webob import Request
from yum import YumBase
from subprocess import *
from config import *

GETTEXT_DOMAIN = "retrace-server"

# filename: max_size (<= 0 unlimited)
ALLOWED_FILES = {
  "coredump": 0,
  "executable": 64,
  "package": 128,
  "os_release": 128,
  "release": 128,
  "vmcore": 0,
}

TASK_RETRACE, TASK_DEBUG, TASK_VMCORE = xrange(3)
TASK_TYPES = [TASK_RETRACE, TASK_DEBUG, TASK_VMCORE]

REQUIRED_FILES = {
  TASK_RETRACE: ["coredump", "executable", "package"],
  TASK_DEBUG:   ["coredump", "executable", "package"],
  TASK_VMCORE:  ["vmcore"],
}

#characters, numbers, dash (utf-8, iso-8859-2 etc.)
INPUT_CHARSET_PARSER = re.compile("^([a-zA-Z0-9\-]+)(,.*)?$")
#en_GB, sk-SK, cs, fr etc.
INPUT_LANG_PARSER = re.compile("^([a-z]{2}([_\-][A-Z]{2})?)(,.*)?$")
#characters allowed by Fedora Naming Guidelines
INPUT_PACKAGE_PARSER = re.compile("^[abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\-\.\_\+]+$")

#2.6.32-201.el6.x86_64
KERNEL_RELEASE_PARSER = re.compile("^(.*)\.([^\.]+)$")

CORE_ARCH_PARSER = re.compile("core file .*(x86-64|80386)")
PACKAGE_PARSER = re.compile("^(.+)-([0-9]+(\.[0-9]+)*-[0-9]+)\.([^-]+)$")
DF_OUTPUT_PARSER = re.compile("^([^ ^\t]*)[ \t]+([0-9]+)[ \t]+([0-9]+)[ \t]+([0-9]+)[ \t]+([0-9]+%)[ \t]+(.*)$")
DU_OUTPUT_PARSER = re.compile("^([0-9]+)")
URL_PARSER = re.compile("^/([0-9]+)/?")

# rpm name parsers
EPOCH_PARSER = re.compile("^(([0-9]+)\:).*$")
ARCH_PARSER = re.compile("^.*(\.([0-9a-zA-Z_]+))$")
RELEASE_PARSER = re.compile("^.*(\-([0-9a-zA-Z\._]+))$")
VERSION_PARSER = re.compile("^.*(\-([0-9a-zA-Z\._\:]+))$")
NAME_PARSER = re.compile("^[a-zA-Z0-9_\.\+\-]+$")

HANDLE_ARCHIVE = {
  "application/x-xz-compressed-tar": {
    "unpack": [TAR_BIN, "xJf"],
    "size": ([XZ_BIN, "--list", "--robot"], re.compile("^totals[ \t]+[0-9]+[ \t]+[0-9]+[ \t]+[0-9]+[ \t]+([0-9]+).*")),
  },

  "application/x-gzip": {
    "unpack": [TAR_BIN, "xzf"],
    "size": ([GZIP_BIN, "--list"], re.compile("^[^0-9]*[0-9]+[^0-9]+([0-9]+).*$")),
  },

  "application/x-tar": {
    "unpack": [TAR_BIN, "xf"],
    "size": (["ls", "-l"], re.compile("^[ \t]*[^ ^\t]+[ \t]+[^ ^\t]+[ \t]+[^ ^\t]+[ \t]+[^ ^\t]+[ \t]+([0-9]+).*$")),
  },
}

REPO_PREFIX = "retrace-"

TASKPASS_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

CONFIG_FILE = "/etc/retrace-server.conf"
CONFIG = {
  "TaskIdLength": 9,
  "TaskPassLength": 32,
  "MaxParallelTasks": 10,
  "MaxPackedSize": 30,
  "MaxUnpackedSize": 600,
  "MinStorageLeft": 10240,
  "DeleteTaskAfter": 120,
  "KeepRawhideLatest": 3,
  "LogDir": "/var/log/retrace-server",
  "RepoDir": "/var/cache/retrace-server",
  "SaveDir": "/var/spool/retrace-server",
  "WorkDir": "/tmp/retrace-server",
  "UseWorkDir": False,
  "RequireHTTPS": True,
  "RequireGPGCheck": True,
  "UseCreaterepoUpdate": False,
  "DBFile": "stats.db",
  "KernelChrootRepo": "http://dl.fedoraproject.org/pub/fedora/linux/releases/16/Everything/$ARCH/os/",
}

STATUS_ANALYZE, STATUS_INIT, STATUS_BACKTRACE, STATUS_CLEANUP, \
STATUS_STATS, STATUS_FINISHING, STATUS_SUCCESS, STATUS_FAIL = xrange(8)

STATUS = [
  "Analyzing crash data",
  "Initializing virtual root",
  "Generating backtrace",
  "Cleaning up virtual root",
  "Saving crash statistics",
  "Finishing task",
  "Retrace job finished successfully",
  "Retrace job failed",
]

def lock(lockfile):
    try:
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL, 0600)
    except OSError as ex:
        if ex[0] == errno.EEXIST:
            return False
        else:
            raise ex

    os.close(fd)
    return True

def unlock(lockfile):
    try:
        if os.path.getsize(lockfile) == 0:
            os.unlink(lockfile)
    except:
        return False

    return True

def read_config():
    parser = ConfigParser.ConfigParser()
    parser.read(CONFIG_FILE)
    for key in CONFIG.keys():
        vartype = type(CONFIG[key])
        if vartype is int:
            get = parser.getint
        elif vartype is bool:
            get = parser.getboolean
        elif vartype is float:
            get = parser.getfloat
        else:
            get = parser.get

        try:
            CONFIG[key] = get("retrace", key)
        except ConfigParser.NoOptionError:
            pass

def free_space(path):
    child = Popen([DF_BIN, "-B", "1", path], stdout=PIPE)
    lines = child.communicate()[0].split("\n")
    for line in lines:
        match = DF_OUTPUT_PARSER.match(line)
        if match:
            return int(match.group(4))

    return None

def dir_size(path):
    child = Popen([DU_BIN, "-sb", path], stdout=PIPE)
    lines = child.communicate()[0].split("\n")
    for line in lines:
        match = DU_OUTPUT_PARSER.match(line)
        if match:
            return int(match.group(1))

    return 0

def unpacked_size(archive, mime):
    command, parser = HANDLE_ARCHIVE[mime]["size"]
    child = Popen(command + [archive], stdout=PIPE)
    lines = child.communicate()[0].split("\n")
    for line in lines:
        match = parser.match(line)
        if match:
            return int(match.group(1))

    return None

def guess_arch(coredump_path):
    child = Popen(["file", coredump_path], stdout=PIPE)
    output = child.communicate()[0]
    match = CORE_ARCH_PARSER.search(output)
    if match:
        if match.group(1) == "80386":
            return "i386"
        elif match.group(1) == "x86-64":
            return "x86_64"

    return None

def guess_release(package, plugins):
    for plugin in plugins:
        match = plugin.guessparser.search(package)
        if match:
            return plugin.distribution, match.group(1)

    return None, None

def get_supported_releases():
    result = []
    yb = YumBase()
    for repo in yb.repos.repos:
        if repo.startswith(REPO_PREFIX):
            result.append(repo[len(REPO_PREFIX):])

    return result

def parse_http_gettext(lang, charset):
    result = lambda x: x
    lang_match = INPUT_LANG_PARSER.match(lang)
    charset_match = INPUT_CHARSET_PARSER.match(charset)
    if lang_match and charset_match:
        try:
            result = gettext.translation(GETTEXT_DOMAIN,
                                         languages=[lang_match.group(1)],
                                         codeset=charset_match.group(1)).gettext
        except:
            pass

    return result

def run_gdb(savedir):
    #exception is caught on the higher level
    exec_file = open(os.path.join(savedir, "crash", "executable"), "r")
    executable = exec_file.read(ALLOWED_FILES["executable"])
    exec_file.close()

    if '"' in executable or "'" in executable:
        raise Exception, "Executable contains forbidden characters"

    chmod = call(["/usr/bin/mock", "shell", "--configdir", savedir,
                  "--", "/bin/chmod", "a+r", "'%s'" % executable])

    if chmod != 0:
        raise Exception, "Unable to chmod the executable"

    batfile = os.path.join(savedir, "gdb.sh")
    with open(batfile, "w") as gdbfile:
        gdbfile.write("gdb -batch -ex 'file %s' "
                      "-ex 'core-file /var/spool/abrt/crash/coredump' "
                      "-ex 'thread apply all backtrace 2048 full' "
                      "-ex 'info sharedlib' "
                      "-ex 'print (char*)__abort_msg' "
                      "-ex 'print (char*)__glib_assert_msg' "
                      "-ex 'info registers' "
                      "-ex 'disassemble'" % executable)

    with open("/dev/null", "w") as null:
        call(["/usr/bin/mock", "--configdir", savedir, "--copyin", batfile, "/var/spool/abrt/gdb.sh"])

        child = Popen(["/usr/bin/mock", "shell", "--configdir", savedir,
                       "--", "su", "mockbuild", "-c", "'/bin/sh /var/spool/abrt/gdb.sh'",
                       # redirect GDB's stderr, ignore mock's stderr
                       "2>&1"], stdout=PIPE, stderr=null)

    backtrace = child.communicate()[0]

    return backtrace

def get_task_est_time(taskdir):
    return 180

def unpack(archive, mime, targetdir=None):
    cmd = list(HANDLE_ARCHIVE[mime]["unpack"])
    cmd.append(archive)
    if not targetdir is None:
        cmd.append("--directory")
        cmd.append(targetdir)

    retcode = call(cmd)
    return retcode

def response(start_response, status, body="", extra_headers=[]):
    start_response(status, [("Content-Type", "text/plain"), ("Content-Length", "%d" % len(body))] + extra_headers)
    return [body]

def get_active_tasks():
    tasks = []

    for filename in os.listdir(CONFIG["SaveDir"]):
        if len(filename) != CONFIG["TaskIdLength"]:
            continue

        try:
            task = RetraceTask(int(filename))
        except:
            continue

        if not task.has_log():
            tasks.append(task.get_taskid())

    return tasks

def parse_rpm_name(name):
    result = {
      "epoch": 0,
      "name": None,
      "version": "",
      "release": "",
      "arch": "",
    }

    # cut off rpm suffix
    if name.endswith(".rpm"):
        name = name[:-4]

    # arch
    match = ARCH_PARSER.match(name)
    if match and match.group(2) in ["i386", "i586", "i686", "x86_64", "noarch"]:
        result["arch"] = match.group(2)
        name = name[:-len(match.group(1))]

    # release
    match = RELEASE_PARSER.match(name)
    if match:
        result["release"] = match.group(2)
        name = name[:-len(match.group(1))]

    # version
    match = VERSION_PARSER.match(name)
    if match:
        result["version"] = match.group(2)
        name = name[:-len(match.group(1))]
    else:
        result["version"] = result["release"]
        result["release"] = None

    # epoch
    match = EPOCH_PARSER.match(name)
    if match:
        result["epoch"] = int(match.group(2))
        name = name[len(match.group(1)):]
    else:
        match = EPOCH_PARSER.match(result["version"])
        if match:
            result["epoch"] = int(match.group(2))
            result["version"] = result["version"][len(match.group(1)):]

    # raw name - verify allowed characters
    match = NAME_PARSER.match(name)
    if match:
        result["name"] = name

    return result

def init_crashstats_db():
    con = sqlite3.connect(os.path.join(CONFIG["SaveDir"], CONFIG["DBFile"]))
    query = con.cursor()
    query.execute("PRAGMA foreign_keys = ON")
    query.execute("""
      CREATE TABLE IF NOT EXISTS
      tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, taskid, package, version,
      arch, starttime NOT NULL, duration NOT NULL, coresize, status NOT NULL)
    """)
    query.execute("""
      CREATE TABLE IF NOT EXISTS
      success(taskid REFERENCES tasks(id), pre NOT NULL, post NOT NULL,
              rootsize NOT NULL)
    """)
    query.execute("""
      CREATE TABLE IF NOT EXISTS
      packages(id INTEGER PRIMARY KEY AUTOINCREMENT,
               name NOT NULL, version NOT NULL)
    """)
    query.execute("""
      CREATE TABLE IF NOT EXISTS
      packages_tasks(pkgid REFERENCES packages(id),
                     taskid REFERENCES tasks(id))
    """)
    query.execute("""
      CREATE TABLE IF NOT EXISTS
      buildids(taskid REFERENCES tasks(id), soname, buildid NOT NULL)
    """)
    query.execute("""
      CREATE TABLE IF NOT EXISTS
      reportfull(requesttime NOT NULL, ip NOT NULL)
    """)
    con.commit()

    return con

def save_crashstats(stats, con=None):
    close = False
    if con is None:
        con = init_crashstats_db()
        close = True

    query = con.cursor()
    query.execute("""
      INSERT INTO tasks (taskid, package, version, arch,
      starttime, duration, coresize, status)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (stats["taskid"], stats["package"], stats["version"],
       stats["arch"], stats["starttime"], stats["duration"],
       stats["coresize"], stats["status"]))

    con.commit()
    if close:
        con.close()

    return query.lastrowid

def save_crashstats_success(statsid, pre, post, rootsize, con=None):
    close = False
    if con is None:
        con = init_crashstats_db()
        close = True

    query = con.cursor()
    query.execute("""
      INSERT INTO success (taskid, pre, post, rootsize)
      VALUES (?, ?, ?, ?)
      """,
      (statsid, pre, post, rootsize))

    con.commit()
    if close:
        con.close()

def save_crashstats_packages(statsid, packages, con=None):
    close = False
    if con is None:
        con = init_crashstats_db()
        close = True

    query = con.cursor()
    for package in packages:
        pkgdata = parse_rpm_name(package)
        if pkgdata["name"] is None:
            continue

        ver = "%s-%s" % (pkgdata["version"], pkgdata["release"])
        query.execute("SELECT id FROM packages WHERE name = ? AND version = ?",
                      (pkgdata["name"], ver))
        row = query.fetchone()
        if row:
            pkgid = row[0]
        else:
            query.execute("INSERT INTO packages (name, version) VALUES (?, ?)",
                          (pkgdata["name"], ver))
            pkgid = query.lastrowid

        query.execute("""
          INSERT INTO packages_tasks (taskid, pkgid) VALUES (?, ?)
          """, (statsid, pkgid))

    con.commit()
    if close:
        con.close()

def save_crashstats_build_ids(statsid, buildids, con=None):
    close = False
    if con is None:
        con = init_crashstats_db()
        close = True

    query = con.cursor()
    for soname, buildid in buildids:
        query.execute("""
          INSERT INTO buildids (taskid, soname, buildid)
          VALUES (?, ?, ?)
          """,
          (statsid, soname, buildid))

    con.commit()
    if close:
        con.close()

def save_crashstats_reportfull(ip, con=None):
    close = False
    if con is None:
        con = init_crashstats_db()
        close = True

    query = con.cursor()
    query.execute("""
      INSERT INTO reportfull (requesttime, ip)
      VALUES (?, ?)
      """,
      (int(time.time()), ip))

    con.commit()
    if close:
        con.close()

class RetraceTask:
    """Represents Retrace server's task."""

    BACKTRACE_FILE = "retrace_backtrace"
    LOG_FILE = "retrace_log"
    PASSWORD_FILE = "password"
    STATUS_FILE = "status"
    TYPE_FILE = "type"

    def __init__(self, taskid=None):
        """Creates a new task if taskid is None,
        loads the task with given ID otherwise."""

        if taskid is None:
            # create a new task
            self._taskid = None
            generator = random.SystemRandom()
            for i in xrange(50):
                taskid = generator.randint(pow(10, CONFIG["TaskIdLength"] - 1),
                                           pow(10, CONFIG["TaskIdLength"]) - 1)
                taskdir = os.path.join(CONFIG["SaveDir"], "%d" % taskid)
                try:
                    os.mkdir(taskdir)
                except OSError as ex:
                    # dir exists, try another taskid
                    if ex[0] == errno.EEXIST:
                        continue
                    # error - re-raise original exception
                    else:
                        raise ex
                # directory created
                else:
                    self._taskid = taskid
                    self._savedir = taskdir
                    break

            if self._taskid is None:
                raise Exception, "Unable to create new task"

            pwdfilepath = os.path.join(self._savedir, RetraceTask.PASSWORD_FILE)
            with open(pwdfilepath, "w") as pwdfile:
                for i in xrange(CONFIG["TaskPassLength"]):
                    pwdfile.write(generator.choice(TASKPASS_ALPHABET))

        else:
            # existing task
            self._taskid = int(taskid)
            self._savedir = os.path.join(CONFIG["SaveDir"], "%d" % self._taskid)
            if not os.path.isdir(self._savedir):
                raise Exception, "The task %d does not exist" % self._taskid

    def get_taskid(self):
        """Returns task's ID"""
        return self._taskid

    def get_savedir(self):
        """Returns task's savedir"""
        return self._savedir

    def get_password(self):
        """Returns task's password"""
        pwdfilename = os.path.join(self._savedir, RetraceTask.PASSWORD_FILE)
        with open(pwdfilename, "r") as pwdfile:
            pwd = pwdfile.read(CONFIG["TaskPassLength"])

        return pwd

    def verify_password(self, password):
        """Verifies if the given password matches task's password."""
        return self.get_password() == password

    def get_age(self):
        """Returns the age of the task in hours."""
        return int(time.time() - os.path.getatime(self._savedir)) / 3600

    def get_type(self):
        """Returns task type. If TYPE_FILE is missing,
        task is considered standard TASK_RETRACE."""
        typefilename = os.path.join(self._savedir, RetraceTask.TYPE_FILE)
        if not os.path.isfile(typefilename):
            return TASK_RETRACE

        with open(typefilename, "r") as typefile:
            # typicaly one digit, max 8B
            result = typefile.read(8)

        return int(result)

    def set_type(self, newtype):
        """Atomically writes given type into TYPE_FILE."""
        tmpfilename = os.path.join(self._savedir,
                                   "%s.tmp" % RetraceTask.TYPE_FILE)
        typefilename = os.path.join(self._savedir, RetraceTask.TYPE_FILE)
        with open(tmpfilename, "w") as tmpfile:
            if newtype in TASK_TYPES:
                tmpfile.write("%d" % newtype)
            else:
                tmpfile.write("%d" % TASK_RETRACE)

        os.rename(tmpfilename, typefilename)

    def has_backtrace(self):
        """Verifies whether BACKTRACE_FILE is present in the task directory."""
        return os.path.isfile(os.path.join(self._savedir,
                                           RetraceTask.BACKTRACE_FILE))

    def get_backtrace(self):
        """Returns None if there is no BACKTRACE_FILE in the task directory,
        BACKTRACE_FILE's contents otherwise."""
        if not self.has_backtrace():
            return None

        btfilename = os.path.join(self._savedir, RetraceTask.BACKTRACE_FILE)
        with open(btfilename, "r") as btfile:
            # max 4 MB
            bt = btfile.read(1 << 22)

        return bt

    def set_backtrace(self, backtrace):
        """Atomically writes given string into BACKTRACE_FILE."""
        tmpfilename = os.path.join(self._savedir,
                                   "%s.tmp" % RetraceTask.BACKTRACE_FILE)
        btfilename = os.path.join(self._savedir,
                                   RetraceTask.BACKTRACE_FILE)

        with open(tmpfilename, "w") as tmpfile:
            tmpfile.write(backtrace)

        os.rename(tmpfilename, btfilename)

    def has_log(self):
        """Verifies whether LOG_FILE is present in the task directory."""
        return os.path.isfile(os.path.join(self._savedir,
                                           RetraceTask.LOG_FILE))

    def get_log(self):
        """Returns None if there is no LOG_FILE in the task directory,
        LOG_FILE's contents otherwise."""
        if not self.has_log():
            return None

        logfilename = os.path.join(self._savedir, RetraceTask.LOG_FILE)
        with open(logfilename, "r") as logfile:
            # max 4 MB
            log = logfile.read(1 << 22)

        return log

    def set_log(self, log, append=False):
        """Atomically writes or appends given string into LOG_FILE."""
        tmpfilename = os.path.join(self._savedir,
                                   "%s.tmp" % RetraceTask.LOG_FILE)
        logfilename = os.path.join(self._savedir,
                                   RetraceTask.LOG_FILE)

        if append:
            if os.path.isfile(logfilename):
                shutil.copyfile(logfilename, tmpfilename)

            with open(tmpfilename, "a") as tmpfile:
                tmpfile.write(log)
        else:
            with open(tmpfilename, "w") as tmpfile:
                tmpfile.write(log)

        os.rename(tmpfilename, logfilename)

    def has_status(self):
        """Verifies whether STATUS_FILE is present in the task directory."""
        return os.path.isfile(os.path.join(self._savedir,
                                           RetraceTask.STATUS_FILE))

    def get_status(self):
        """Returns None if there is no STATUS_FILE in the task directory,
        an integer status code otherwise."""
        if not self.has_status():
            return None

        statusfilename = os.path.join(self._savedir, RetraceTask.STATUS_FILE)
        with open(statusfilename, "r") as statusfile:
            # typically one digit, max 8B
            status = statusfile.read(8)

        return int(status)

    def set_status(self, statuscode):
        """Atomically writes given statuscode into STATUS_FILE."""
        tmpfilename = os.path.join(self._savedir,
                                   "%s.tmp" % RetraceTask.STATUS_FILE)
        statusfilename = os.path.join(self._savedir,
                                      RetraceTask.STATUS_FILE)

        with open(tmpfilename, "w") as tmpfile:
            tmpfile.write("%d" % statuscode)

        os.rename(tmpfilename, statusfilename)

    def clean(self):
        """Removes all files and directories except for BACKTRACE_FILE,
        LOG_FILE, PASSWORD_FILE and STATUS_FILE from the task directory."""
        with open("/dev/null", "w") as null:
            if os.path.isfile(os.path.join(self._savedir, "default.cfg")) and \
               os.path.isfile(os.path.join(self._savedir, "site-defaults.cfg")) and \
               os.path.isfile(os.path.join(self._savedir, "logging.ini")):
                retcode = call(["/usr/bin/mock", "--configdir", self._savedir, "--scrub=all"],
                               stdout=null, stderr=null)

        for f in os.listdir(self._savedir):
            if f != RetraceTask.BACKTRACE_FILE and \
               f != RetraceTask.LOG_FILE and \
               f != RetraceTask.PASSWORD_FILE and \
               f != RetraceTask.STATUS_FILE:
                path = os.path.join(self._savedir, f)
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                except:
                    # clean as much as possible
                    # ToDo advanced handling
                    pass

    def remove(self):
        """Completely removes the task directory."""
        shutil.rmtree(self._savedir)

### read config on import ###
read_config()
