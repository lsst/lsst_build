#############################################################################
# Git support

import asyncio
import subprocess


class GitError(Exception):
    def __init__(self, returncode, cmd, output, stderr):
        self.returncode = returncode
        self.cmd = cmd
        self.output = output
        self.stderr = stderr

    def __str__(self):
        return "Command '%s' returned non-zero exit status %d.\nstdout:\n%s\nstderr:\n%s" % (self.cmd,
                                                                                             self.returncode,
                                                                                             self.output,
                                                                                             self.stderr)


class Git:
    def __init__(self, cwd=None):
        self.cwd = cwd

    @staticmethod
    async def clone(*args, **kwargs):
        return await Git()('clone', *args, **kwargs)

    async def __call__(self, *args, **kwargs):
        # Run git with the given arguments, returning stdout.

        return_status = kwargs.get("return_status", False)

        # force all cli args into strings
        cmd = ['git'] + [str(x) for x in args]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd
        )
        (stdout, stderr) = await process.communicate()
        stdout = stdout.decode('utf-8')
        stderr = stderr.decode('utf-8')
        retcode = process.returncode

        if retcode and not return_status:
            raise GitError(retcode, cmd, stdout, stderr)

        return stdout.rstrip() if not return_status else (stdout.rstrip(), retcode)

    def _sync(self, *args, **kwargs):
        # Run git with the given arguments, returning stdout.
        return_status = kwargs.get("return_status", False)

        # force all cli args into strings
        cmd = ['git'] + [str(x) for x in args]

        process = subprocess.Popen(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd
        )
        (stdout, stderr) = process.communicate()
        stdout = stdout.decode('utf-8')
        stderr = stderr.decode('utf-8')
        retcode = process.returncode

        if retcode and not return_status:
            raise GitError(retcode, cmd, stdout, stderr)

        return stdout.rstrip() if not return_status else (stdout.rstrip(), retcode)

    async def checkout(self, *args, **kwargs):
        return await self('checkout', *args, **kwargs)

    async def rev_parse(self, *args, **kwargs):
        return await self('rev-parse', *args, **kwargs)

    async def reset(self, *args, **kwargs):
        return await self('reset', *args, **kwargs)

    async def clean(self, *args, **kwargs):
        return await self('clean', *args, **kwargs)

    async def fetch(self, *args, **kwargs):
        return await self('fetch', *args, **kwargs)

    async def pull(self, *args, **kwargs):
        return await self('pull', *args, **kwargs)

    async def commit(self, *args, **kwargs):
        return await self('commit', *args, **kwargs)

    async def add(self, *args, **kwargs):
        return await self('add', *args, **kwargs)

    async def tag(self, *args, **kwargs):
        return await self('tag', *args, **kwargs)

    async def describe(self, *args, **kwargs):
        return await self('describe', *args, **kwargs)

    async def lfs(self, *args, **kwargs):
        return await self('lfs', *args, **kwargs)

    def sync_checkout(self, *args, **kwargs):
        return self._sync('checkout', *args, **kwargs)

    def sync_rev_parse(self, *args, **kwargs):
        return self._sync('rev-parse', *args, **kwargs)

    def sync_reset(self, *args, **kwargs):
        return self._sync('reset', *args, **kwargs)

    def sync_clean(self, *args, **kwargs):
        return self._sync('clean', *args, **kwargs)

    def sync_fetch(self, *args, **kwargs):
        return self._sync('fetch', *args, **kwargs)

    def sync_pull(self, *args, **kwargs):
        return self._sync('pull', *args, **kwargs)

    def sync_commit(self, *args, **kwargs):
        return self._sync('commit', *args, **kwargs)

    def sync_add(self, *args, **kwargs):
        return self._sync('add', *args, **kwargs)

    def sync_tag(self, *args, **kwargs):
        return self._sync('tag', *args, **kwargs)

    def sync_describe(self, *args, **kwargs):
        return self._sync('describe', *args, **kwargs)

    def sync_lfs(self, *args, **kwargs):
        return self._sync('lfs', *args, **kwargs)
