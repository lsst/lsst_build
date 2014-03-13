#############################################################################
# Git support

import subprocess

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
        #print "---> ", " ".join(cmd)

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.cwd)
        (stdout, stderr) = process.communicate()
        retcode = process.poll()

        if retcode and not return_status:
            #print cmd
            #print stdout
            #print stderr
            raise GitError(retcode, cmd, stdout, stderr)

        return stdout.rstrip() if not return_status else (stdout.rstrip(), retcode)

    def checkout(self, *args, **kwargs):
        return self('checkout', *args, **kwargs)

    def rev_parse(self, *args, **kwargs):
        return self('rev-parse', *args, **kwargs)

    def reset(self, *args, **kwargs):
        return self('reset', *args, **kwargs)

    def clean(self, *args, **kwargs):
        return self('clean', *args, **kwargs)

    def fetch(self, *args, **kwargs):
        return self('fetch', *args, **kwargs)

    def pull(self, *args, **kwargs):
        return self('pull', *args, **kwargs)

    def commit(self, *args, **kwargs):
        return self('commit', *args, **kwargs)

    def add(self, *args, **kwargs):
        return self('add', *args, **kwargs)

    def tag(self, *args, **kwargs):
        return self('tag', *args, **kwargs)

    def describe(self, *args, **kwargs):
        return self('describe', *args, **kwargs)
