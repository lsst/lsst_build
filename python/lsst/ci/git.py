#############################################################################
# Git support

import subprocess
import time
import sys, os
import contextlib

class GitError:
    def __init__(self, returncode, cmd, output, stderr):
        self.returncode = returncode
        self.cmd = cmd
        self.output = output
        self.stderr = stderr

    def __str__(self):
        return "Command '%s' returned non-zero exit status %d." % (self.cmd, self.returncode)

class Git:
    def __init__(self, cwd=None):
        self.cwd = cwd

    @staticmethod
    def clone(*args, **kwargs):
        return Git()('clone', *args, **kwargs)

    def __call__(self, *args, **kwargs):
        # Run git with the given arguments, returning stdout.

        return_status = kwargs.get("return_status", False)

        cmd = ('git',) + args

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.cwd)
        (stdout, stderr) = process.communicate()
        retcode = process.poll()

        if retcode and not return_status:
            raise GitError(retcode, cmd, stdout, stderr)

        return stdout.rstrip() if not return_status else (stdout.rstrip(), retcode)

    def checkout(self, *args, **kwargs):
        return self('checkout', *args, **kwargs)

    def rev_parse(self, *args, **kwargs):
        return self('rev-parse', *args, **kwargs)

    def reflog(self, *args, **kwargs):
        return self('reflog', *args, **kwargs)

    def reset(self, *args, **kwargs):
        return self('reset', *args, **kwargs)

    def clean(self, *args, **kwargs):
        return self('clean', *args, **kwargs)

    def fetch(self, *args, **kwargs):
        return self('fetch', *args, **kwargs)

    def pull(self, *args, **kwargs):
        return self('pull', *args, **kwargs)

    def merge(self, *args, **kwargs):
        return self('merge', *args, **kwargs)

    def log(self, *args, **kwargs):
        return self('log', *args, **kwargs)

    def push(self, *args, **kwargs):
        return self('push', *args, **kwargs)

    def commit(self, *args, **kwargs):
        return self('commit', *args, **kwargs)

    def add(self, *args, **kwargs):
        return self('add', *args, **kwargs)

    def tag(self, *args, **kwargs):
        return self('tag', *args, **kwargs)

    def describe(self, *args, **kwargs):
        return self('describe', *args, **kwargs)

    def isclean(self):
	dirty = self.describe('--always', '--dirty=-prljavonakraju').endswith("-prljavonakraju")
	untracked = self('ls-files', '--others', '--exclude-standard') != ""
        return not dirty and not untracked

@contextlib.contextmanager
def transaction(git, *args, **kwargs):
    pull = kwargs.get('pull', True)
    push = kwargs.get('push', True)

	# refuse to	run if the working directory is not clean
    if not git.isclean():
        raise Exception('working directory is not clean, refusing to proceed [%s].' % git.cwd)

    # record the original branch and sha1 that we're on
    ref0 = git('symbolic-ref', 'HEAD')
    if not ref0.startswith('refs/heads/'):
        raise Exception('working directory HEAD is not a branch (%s).' % ref)
    ref = ref0[11:]
    sha1 = git.rev_parse("HEAD")

    try:
        if pull:
            print "pulling %s" % os.path.basename(git.cwd)
            git.pull(*args)

        yield

        # push only if there's something new to be pushed
        if push and (ref0 != git('symbolic-ref', 'HEAD') or sha1 != git.rev_parse("HEAD")):
            print "pushing %s" % os.path.basename(git.cwd)
            git.push()
    except:
        # check out the original branch, rebase to saved hash,
        # and clean up the working directory
        git.reset("--hard")
        git.checkout(ref)
        git.reset("--hard", sha1)
        git.clean("-d", "-f", "-q")

        raise
    finally:
        # Code using transactions must leave the repository clean
        assert git.isclean()
