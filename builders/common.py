""" Contains renderers and build steps that are useful to more than one
build factory. """

__all__ = ["rtdist_lock", "sse2_flag", "threads_flag", "buildtype_flag",
           "common_flags", "MakeTorrent"]

from buildbot.steps.shell import SetPropertyFromCommand, ShellCommand
from buildbot.steps.master import MasterShellCommand
from buildbot.steps.transfer import DirectoryUpload
from buildbot.process.properties import Interpolate, Property, renderer
from buildbot.locks import MasterLock

import config

# Define a lock so that only one builder can update the rtdist at a time.
rtdist_lock = MasterLock('rtdist')

@renderer
def sse2_flag(props):
    "Determines the SSE2 flag based on the requested architecture."

    if "macosx" in props["buildername"]:
        # All Intel Macs have SSE2, I think
        return ["--use-sse2"]

    if props["arch"] in ("amd64", "x86_64"):
        return ["--use-sse2"]
    else:
        # Let's not use Eigen in 32-bit builds.  It's of questionable value
        # when we don't use SSE2, and it makes the Windows build too slow.
        return ["--no-sse2", "--no-eigen"]

@renderer
def threads_flag(props):
    "Determines the --threads flag to use."

    if props.getProperty("threads", 0) > 1:
        return "--threads=%d" % (props["threads"])
    else:
        return ""

@renderer
def buildtype_flag(props):
    "Determines whether to use --runtime, --rtdist, or neither."

    if "buildtype" in props:
        if props["buildtype"] == "runtime":
            return "--runtime"
        elif props["buildtype"] == "rtdist":
            return "--rtdist"

    return ""

@renderer
def refspec(props):
    "The base tag from which to count revision number."

    # Needs to be fixed if we ever request building 2.0.0, probably by picking
    # the point at which 2.0 branched off.
    version = list(map(int, props["version"].split('.')))
    if version[2] > 0:
        # Measure since the last minor release.
        version[2] -= 1
    else:
        # Measure since the last major release.
        version[1] -= 1

    if "branch" in props and props["branch"] and props["branch"].startswith("release/"):
        # This is a release branch, so don't look at the merge base.
        base = ""
    else:
        # Count commits until the last point on the master branch, after which
        # we encode further revisions via the local version tag.
        base = props["merge-base"]

    return "v{0}.{1}.{2}..{3}".format(version[0], version[1], version[2], base)

@renderer
def whl_version(props):
    "Determine which PEP 440 version string a .whl package should have."

    if "buildtype" in props and props["buildtype"] in ("rtdist", "runtime"):
        # Not building a wheel.
        return props["version"]

    if props["revision"] == "v" + props["version"]:
        # We requested building a particular version tag, so this must be a
        # release.
        return props["version"]

    version = tuple(map(int, props["version"].split('.')))

    # Was this commit branched off from the main branch?
    local = ""
    if props["merge-base"] != props["got_revision"] and not props["branch"].startswith("release/"):
        # Add a local tag indicating that this has unofficial changes.
        local += "+g" + props["got_revision"][:7]

    # Is this a post-release build?  Check using the output of "git describe",
    # which contains the last release tag plus the number of commits since it.
    if "commit-description" in props:
        desc = props["commit-description"].split('-')

        if desc[0] == "v{0}.{1}.{2}".format(*version):
            if len(desc) == 1:
                # This is exactly this release.
                return props["version"]
            else:
                # This is a post-release.
                return "{0}.post{1}{2}".format(props["version"], desc[1], local)

    # No, it's a pre-release.  Make a version tag based on the number of
    # commits since the last major release.
    return "{0}.dev{1}{2}".format(props["version"], props["commit-index"], local)

@renderer
def python_abi(props):
    "Determine which Python ABI a build uses."

    # If the builder didn't set this property, determine it from the Python
    # version.
    if "python-version" in props and props["python-version"]:
        version = props["python-version"].replace('.', '')
    else:
        version = "27"

    if "python-abi" in props and props["python-abi"]:
        return "cp{0}-{1}".format(version, props["python-abi"])

    abi_tag = "cp{0}-cp{0}m".format(version)
    if version[0] == '2':
        # This assumes we're on Linux.  On other platforms, set the python-abi
        # property appropriately, please.
        abi_tag += 'u'

    return abi_tag

@renderer
def whl_filename(props):
    "Determines the name of a .whl file for uploading."

    abi = python_abi.getRenderingFor(props)
    platform = props["platform"].replace('-', '_')
    version = whl_version.getRenderingFor(props)
    return "panda3d-{0}-{1}-{2}.whl".format(version, abi, platform)

@renderer
def whl_upload_filename(props):
    "Determines the upload location of a .whl file on the master."

    return '/'.join((config.downloads_dir,
                     props["got_revision"],
                     whl_filename.getRenderingFor(props)))

@renderer
def common_flags(props):
    "Returns makepanda flags common to all builders."

    flags = [
        "--verbose",
        "--nocolor",
        sse2_flag.getRenderingFor(props),
        "--distributor=" + config.distributor,
        "--git-commit=" + props["got_revision"],
    ]

    if props.getProperty("threads", 0) > 1:
        flags.append("--threads=%d" % (props["threads"]))

    if props.getProperty("clean"):
        flags.append("--clean")

    buildtype = "sdk"

    if "buildtype" in props:
        buildtype = props["buildtype"] or "sdk"

    if buildtype != "sdk":
        flags.append("--" + buildtype)

    if buildtype == "rtdist":
        flags.append("--host=https://runtime.panda3d.org/")

    elif buildtype == "sdk":
        # Only build the .p3d deployment tools on a branch that's already
        # had a release.  Bit of a hacky way to determine that.
        major_version = '.'.join(props["version"].split('.', 2)[:2])
        if props.getProperty("commit-description", "").startswith('v' + major_version + '.'):
            flags.append("--host=https://runtime.panda3d.org/")

    return flags

@renderer
def rtdist_staging_dir(props):
    "The directory to which the rtdist is uploaded."

    return '%s/%s-%d' % (config.staging_dir, props['buildername'], props['buildnumber'])

# Steps to figure out which .whl version to use.
whl_version_steps = [
    # Get the point of last merge between this commit and master.
    # Buildbot is very selective about its fetches, so we need to make
    # sure we update the origin/master ref ourselves.
    ShellCommand(name="update-ref", command=[
        "git", "fetch", "origin", "+refs/heads/master:refs/remotes/origin/master"]),
    SetPropertyFromCommand("merge-base", command=[
        "git", "merge-base", "origin/master", Property("got_revision")],
        haltOnFailure=True),

    # Count the number of commits between the last release and the last merge.
    SetPropertyFromCommand("commit-index", command=[
        "git", "rev-list", "--count", refspec],
        haltOnFailure=True),
]

# Steps to publish the rtdist.
publish_rtdist_steps = [
    # Upload the stage directory.
    DirectoryUpload(slavesrc="built/stage", masterdest=rtdist_staging_dir,
                    haltOnFailure=True),

    # Run pmerge.
    MasterShellCommand(name="pmerge", command=[
        config.pmerge_bin, "-i", config.runtime_dir, rtdist_staging_dir])
]

def MakeTorrent(filename, **kwargs):
    "Pseudo-class.  This build step creates a torrent on the master."

    return MasterShellCommand(command=[
        "transmission-create",
        "-t", "udp://tracker.publicbt.com:80",
        "-t", "udp://tracker.opentrackr.org:1337/announce",
        "-t", "http://tracker.bittorrent.am/announce",
        "-t", "udp://tracker.sktorrent.net:6969",
        "-o", Interpolate("%s.torrent", filename),
        filename], **kwargs)

def SeedTorrent(filename, **kwargs):
    """Pseudo-class.  This build step adds a torrent on the master.
    Requires a .netrc file to be present on the master containing the
    transmission-remote authentication credentials.
    """

    return MasterShellCommand(command=[
        "transmission-remote",
        "-a", Interpolate("%s.torrent", filename),
        "--find", filename], **kwargs)
