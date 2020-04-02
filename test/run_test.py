"""Test retrace-server before make install.

run: python run_test.py [--coredump="/path/to/the/coredump"
        --executable="executable" --package="package"]
        [--delete_repo] [--dont_create_repo]
        --release="Fedora release 24 (Twenty Four)"
When coredump path is passed, executbale and package are required
When no coredump path is passed, coredump will be generated and executable
   and package are ignored
Warning: using delete_repo will remove repo, even though it was not created
        by this script, be careful if you use your own repo
Note: might need sudo privileges due to using /var/spool/
"""

import os
import grp
import argparse
import sys
import shutil

from pathlib import Path
from subprocess import DEVNULL, PIPE, run

from retrace.retrace import *
from retrace.retrace_worker import RetraceWorker
from retrace.config import Config

def fatal_error(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)
    sys.exit(1)

def error(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def create_repo(packages, releaseid, version):
    """Create repo with packages passed in arg packages."""
    print("Creating repo")

    conf = Config()

    repo_path = Path(conf["RepoDir"], releaseid)
    if not repo_path.is_dir():
        repo_path.mkdir()
    needed_packages =\
                  ["abrt-addon-ccpp", "shadow-utils", "gdb", "rpm"] + packages

    cmd = 'dnf --releasever={0} --enablerepo=\*debuginfo\* -y\
           --installroot={1} download --resolve --destdir {2} {3}'\
          .format(version, repo_path, repo_path, " ".join(needed_packages))

    c = run(cmd, shell=True, stdout=PIPE, stderr=PIPE, encoding='utf-8')
    stdout, stderr = c.stdout, c.stderr
    #NOTE if error-msg states: "Failed to synchronize cache for repo"
        #easiest thing is to go /etc/yum.repos.d/failed_repo and comment
        #line "skip_if_unavailable=False"
    if c.returncode != 0:
        fatal_error("Command has failed:\n", stderr)

    # create repo from downloaded packages
    createrepo_cmd = ["createrepo", repo_path]
    child = run(createrepo_cmd, stdout=DEVNULL, stderr=PIPE, encoding='utf-8')
    if child.returncode:
        error("ERROR: during creating repo this errors occurred:", child.stderr)


def delete_repo(releaseid):
    """Remove repo.

    Note: deletes repo even though, if was not created by this script
    """
    print("Deleting repo")
    conf = Config()
    repo_path = Path(conf["RepoDir"], releaseid)
    if repo_path.is_dir():
        shutil.rmtree(repo_path)


def generate_coredump():
    """Generate coredump.

    This is done by calling gcore on this script.
    """
    print("Creating coredump")
    # use gcore on this process
    gcore_cmd = ['gcore', '-o', "/var/tmp/coredump", str(os.getpid())]
    child = run(gcore_cmd, stdout=PIPE, stderr=PIPE, encoding='utf-8')
    stdout = child.stdout
    if child.returncode:
        fatal_error("Following error was generated by gcore:", child.stderr)

    # get only second-to-last line form output - there is filename mentioned
    lastline = stdout.splitlines()[-2]
    # get the last word, that should be coredump path
    corepath = Path(lastline[lastline.rfind(" ")+1:])
    if str(corepath.parent) != "/var/tmp":
        fatal_error("Corefile was not generated")

    # rename it, so it is called coredump, not coredump.XXXXX
    corepath.rename(corepath.with_suffix(""))
    corepath = corepath.with_suffix("")

    # find in which package is python
    exe_link = os.readlink('/proc/self/exe')
    rpm_cmdline = 'rpm -qf '+exe_link
    child = run(rpm_cmdline, shell=True, stdout=PIPE, stderr=DEVNULL, encoding='utf-8')
    package = child.stdout
    if child.returncode:
        fatal_error("rpm for python was not found")
    package = package[:package.rfind(".")]
    return (corepath, package, sys.executable)


def create_local_config(change_repo_dir):
    """Create local config file, change what is needed and edit env var."""
    # create local copy of set configuration file
    # read current configuration file
    config_path = Path(os.environ.get('RETRACE_SERVER_CONFIG_PATH'))
    new_conf_path = '/var/tmp/retrace-server.conf'

    with config_path.open() as org_file:
        with Path(new_conf_path).open("w") as new_file:
            for line in org_file:
                if line.startswith("RepoDir") and not change_repo_dir:
                    line = 'RepoDir = /var/tmp/retrace-server-repo\n'
                if line.startswith("RequireGPGCheck"):
                    line = 'RequireGPGCheck = 0\n'
                if line.startswith("SaveDir"):
                    line = 'SaveDir = /var/tmp/retrace-server-spool\n'
                if line.startswith("AuthGroup"):
                    line = 'AuthGroup = {0}\n'.format(grp.getgrgid(os.getgid()).
                                                      gr_name)
                new_file.write(line)

    # edit env variable RETRACE_SERVER_CONFIG_PATH
    os.environ["RETRACE_SERVER_CONFIG_PATH"] = new_conf_path

    try:
        Path("/var/tmp/retrace-server-spool").mkdir()
    except:
        pass

    try:
        Path("/var/tmp/retrace-server-repo").mkdir()
    except:
        pass


# Parse arguments
parser = argparse.ArgumentParser(description="Run test for retrace-server.")
parser.add_argument("--coredump", help="path to the coredump, if not \
                                       specified, will be generated")
parser.add_argument("--release", help="os_release,\
                                      default=Fedora release 24 (Twenty Four)")
parser.add_argument("--executable", help="path to the executable")
parser.add_argument("--package", help="name of package")
parser.add_argument("--delete_repo", help="remove repo after bt generation",
                    action="store_true")
parser.add_argument("--dont_create_repo", help="don't create repo before bt\
                    generation", action="store_true")
parser.add_argument("--interactive", help="run as interactive task, that means\
                    that task will not be deleted at the end.",
                    action="store_true")
args = parser.parse_args()

# set default values
os_release = args.release if args.release else "Fedora release 24 (Twenty Four)"
dont_create_repo_arg = args.dont_create_repo
delete_repo_arg = args.delete_repo

# create edited copy of configuration file
create_local_config(dont_create_repo_arg)

# if coredump specified, parse packages and executable
if args.coredump:
    coredump_path = args.coredump
    package = args.package
    executable = args.executable
    if not package or not executable:
        fatal_error("Coredump specified, must specify package and executable")

# if not coredump specified, generate it
else:
    coredump_path, package, executable = generate_coredump()

# create task
task = RetraceTask()
print("Task ID: "),
print(task.get_taskid())

if args.interactive:
    task.set_type(TASK_RETRACE_INTERACTIVE)
else:
    task.set_type(TASK_RETRACE)
task.set("custom_package", package)
task.set("custom_executable", executable)
task.set("custom_os_release", os_release)

crashdir = task.get_savedir() / "crash"
crashdir.mkdir()
# copy coredump to the retrace task directory
shutil.copy(coredump_path, crashdir)

if not dont_create_repo_arg:
    corepath = crashdir / "coredump"
    worker = RetraceWorker(task)
    # read architecture from coredump
    arch = worker.read_architecture(None, corepath)

    # read release, distribution and version
    (release, distribution, version, _) = worker.read_release_file(crashdir,
                                                                   package)
    releaseid = "%s-%s-%s" % (distribution, version, arch)

    # find missing packages
    packages, missing_unparsed = worker.read_packages(crashdir, releaseid,
                                                      package, distribution)
    #find what provides missing parts
    missing = []
    cmd = ["rpm", "-qf"]
    for part in missing_unparsed:
        cmd.append(part[0])
    child = run(cmd, stdout=PIPE, stderr=DEVNULL, encoding='utf-8')
    missing = child.stdout
    # create unique list
    missing = [] if not missing else list(set(missing.split("\n")))
    # add crash package
    missing.append(package)
    # add all packages, that were found but not marked as missing
    missing = missing + packages

    # create repo
    create_repo(missing, releaseid, version)

# instead of task.start() (a few missing lines, should be added?)
cmdline = ["retrace-server-worker", "%d" % task.get_taskid(), "--foreground"]
run(cmdline)

# check if backtrace was generated
if not task.has_backtrace():
    fatal_error("There is no backtrace for the specified task")

else:
    print("Backtrace is ready")
    with Path("../backtrace").open("w") as backtrace_file:
        bt = task.get_backtrace()
        backtrace_file.write(bt)

if delete_repo_arg:
    delete_repo(releaseid)
